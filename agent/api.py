"""Authenticated loopback task API. Accepted != executed != verified completion."""
import argparse
import concurrent.futures
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import urllib.parse
import urllib.request
import urllib.error
import time
from .engine import Store, Engine, Conflict
from .provider import account_status, UPSTREAM, UPSTREAM_COMMIT, MODEL, MODELS


def public(task, full=False):
    if full:return task
    return {k:v for k,v in task.items() if k not in ('transcript','pending','receipts','answer')}


def handler(store, engine, token):
    runtime_code_sha256=hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in ('api.py','engine.py','provider.py','mcp.mjs'))).hexdigest()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,body,cookie=None):
            raw=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
            if cookie:self.send_header('Set-Cookie',cookie)
            self.end_headers();self.wfile.write(raw)
        def do_GET(self):self.dispatch('GET')
        def do_POST(self):self.dispatch('POST')
        def dispatch(self,method):
            url=urllib.parse.urlsplit(self.path)
            if method=='GET' and url.path=='/':
                raw=Path(__file__).with_name('index.html').read_bytes();self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();return self.wfile.write(raw)
            if method=='POST' and self.headers.get('Origin'):
                if urllib.parse.urlsplit(self.headers['Origin']).netloc != self.headers.get('Host'):
                    return self.send(403,{'error':'다른 사이트에서 보낸 요청입니다. 작업은 제출하지 않았습니다.'})
            if method=='POST' and url.path=='/login':
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if size<1 or size>8192:raise ValueError('Invalid login request')
                    body=json.loads(self.rfile.read(size))
                    request=urllib.request.Request('http://127.0.0.1:7872/api/login',
                        data=json.dumps({'password':body.get('password','')}).encode(),headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(request,timeout=15) as response:response.read()
                    expiry=str(int(time.time())+7*86400)
                    signature=hmac.new(token.encode(),expiry.encode(),hashlib.sha256).hexdigest()
                    return self.send(200,{'ok':True},f'agent_session={expiry}.{signature}; Path=/agent/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800')
                except urllib.error.HTTPError as exc:
                    status=exc.code;exc.close();return self.send(status,{'error':'기존 웹 접속 암호를 확인하세요.' if status==401 else '기존 웹 로그인 서버 응답을 확인하세요.'})
                except Exception:return self.send(400,{'error':'로그인을 확인하지 못했습니다. 기존 웹 서버 연결 상태를 확인하세요.'})
            authorized=secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+token)
            if not authorized:
                from http.cookies import SimpleCookie, CookieError
                try:
                    cookies=SimpleCookie(self.headers.get('Cookie',''));value=cookies['agent_session'].value
                    expiry,signature=value.split('.',1)
                    expected=hmac.new(token.encode(),expiry.encode(),hashlib.sha256).hexdigest()
                    authorized=int(expiry)>time.time() and secrets.compare_digest(signature,expected)
                except (KeyError,ValueError,CookieError):pass
            if not authorized:
                return self.send(401,{'error':'Authentication required; no task submitted'})
            try:
                url=urllib.parse.urlsplit(self.path);parts=url.path.strip('/').split('/')
                query=urllib.parse.parse_qs(url.query)
                body={}
                if method=='POST':
                    size=int(self.headers.get('Content-Length','0'))
                    if size<0 or size>16*1024*1024:return self.send(413,{'error':'Request exceeds 16 MiB; use a workspace file for large inputs'})
                    body=json.loads(self.rfile.read(size) or b'{}')
                    if not isinstance(body,dict):raise ValueError('JSON object required')
                if parts==['health']:
                    return self.send(200,dict(service='codexpro-autonomous-agent',default_model=MODEL,models=MODELS,upstream=UPSTREAM,upstream_commit=UPSTREAM_COMMIT,
                                              approval_mode='authorized-vm-autonomous',paid_model_api=False,runtime_code_sha256=runtime_code_sha256))
                if parts==['v1','accounts'] and method=='GET':
                    model=query.get('model',[MODEL])[0]
                    if model not in MODELS:raise ValueError('Unsupported model')
                    with concurrent.futures.ThreadPoolExecutor(3) as pool:result=list(pool.map(lambda a:account_status(a,model),'abc'))
                    return self.send(200,{'accounts':result})
                if parts==['v1','tasks']:
                    if method=='GET':return self.send(200,{'tasks':[public(t) for t in store.all()]})
                    task,created=store.create(body,self.headers.get('Idempotency-Key'))
                    if task['state'] in ('queued','running'):engine.start(task['id'])
                    return self.send(202 if created else 200,dict(task=public(task),created=created,status_url='/v1/tasks/'+task['id']))
                if len(parts)>=3 and parts[:2]==['v1','tasks']:
                    key=parts[2];task=store.get(key)
                    if len(parts)==3 and method=='GET':return self.send(200,public(task))
                    if parts[3:]==['events'] and method=='GET':
                        events=store.events(key,int(query.get('after',['0'])[0]));return self.send(200,dict(events=events,next_after=events[-1]['seq'] if events else None))
                    if len(parts)==5 and parts[3]=='receipts' and method=='GET':
                        receipt=task['receipts'][parts[4]]
                        text=json.dumps(receipt,ensure_ascii=False);offset=max(0,int(query.get('offset',['0'])[0]));length=min(30000,max(1,int(query.get('length',['12000'])[0])))
                        return self.send(200,dict(receipt_id=parts[4],text=text[offset:offset+length],total_characters=len(text),next_offset=offset+length if offset+length<len(text) else None))
                    if parts[3:]==['cancel'] and method=='POST':
                        if task['state'] in ('completed','cancelled'):return self.send(200,public(task))
                        store.save(key,cancelled=True,state='cancel_requested')
                        engine.start(key);return self.send(202,public(store.get(key)))
                    if parts[3:]==['resume'] and method=='POST':
                        return self.send(202,public(engine.resume(key,body.get('conversation_id'),body.get('message'))))
                self.send(404,{'error':'Unknown endpoint; no task submitted'})
            except Conflict as exc:self.send(409,{'error':str(exc)})
            except KeyError:self.send(404,{'error':'Task or receipt not found'})
            except (ValueError,TypeError) as exc:self.send(400,{'error':str(exc),'submitted':False})
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception as exc:self.send(500,{'error_type':type(exc).__name__,'outcome':'unknown; query original task ID before retrying'})
    return Handler


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',default=str(Path.home()/'.local/state/codexpro-agent'))
    parser.add_argument('--port',type=int,default=7884);args=parser.parse_args()
    os.umask(0o077);store=Store(args.state)
    lock=open(store.root/'service.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    token_path=store.root/'api-token'
    if not token_path.exists():
        with open(token_path,'x') as output:output.write(secrets.token_urlsafe(48))
    if token_path.stat().st_mode&0o077:raise RuntimeError('API token file must be private (0600)')
    token=token_path.read_text().strip();engine=Engine(store)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler(store,engine,token))
    for task in store.all():
        if task['state'] in ('queued','running','cancel_requested'):engine.start(task['id'])
    print(f'Autonomous task API listening on 127.0.0.1:{args.port}; model={MODEL}',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
