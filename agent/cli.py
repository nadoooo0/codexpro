"""Local CLI reads the private token; users need not paste credentials into prompts."""
import argparse
import json
from pathlib import Path
import urllib.request
import urllib.error


def main():
    parser=argparse.ArgumentParser(description='GPT-6 Pro 자율 작업 API')
    parser.add_argument('--url',default='http://127.0.0.1:7884')
    parser.add_argument('--token-file',default=str(Path.home()/'.local/state/codexpro-agent/api-token'))
    sub=parser.add_subparsers(dest='action',required=True)
    run=sub.add_parser('run');run.add_argument('--account',choices=['a','b','c'],required=True)
    run.add_argument('--cwd',required=True);run.add_argument('--id',required=True);run.add_argument('goal')
    run.add_argument('--model',choices=['gpt-6-pro','gpt-6-astra-wm'],default='gpt-6-pro');run.add_argument('--effort')
    for name in ('status','events','cancel','resume'):
        command=sub.add_parser(name);command.add_argument('id')
        if name=='resume':command.add_argument('--conversation-id');command.add_argument('--message')
        if name=='events':command.add_argument('--after',type=int,default=0)
    sub.add_parser('accounts');sub.add_parser('list')
    args=parser.parse_args()
    try:token=Path(args.token_file).read_text().strip()
    except OSError:
        parser.exit(1,'실행기 연결 정보가 아직 준비되지 않았습니다. codexpro-agent.service 상태를 확인하세요.\n')
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    body=None
    if args.action=='run':
        path='/v1/tasks';headers['Idempotency-Key']=args.id
        body={'account':args.account,'cwd':args.cwd,'goal':args.goal,'model':args.model}
        if args.effort:body['effort']=args.effort
    elif args.action=='accounts':path='/v1/accounts'
    elif args.action=='list':path='/v1/tasks'
    else:
        path='/v1/tasks/'+args.id
        if args.action=='events':path+='/events?after='+str(args.after)
        if args.action in ('resume','cancel'):
            path+='/'+args.action;body={}
            if args.action=='resume' and args.conversation_id:body['conversation_id']=args.conversation_id
            if args.action=='resume' and args.message:body['message']=args.message
    request=urllib.request.Request(args.url+path,headers=headers,data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request,timeout=65) as response:result=json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode());raise SystemExit(1)
    except (urllib.error.URLError,TimeoutError) as exc:
        parser.exit(1,f'연결 응답을 확인하지 못했습니다 ({type(exc).__name__}). 새 작업 번호로 다시 제출하지 말고 기존 번호로 상태를 확인하세요.\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
