"""Deterministic fault/recovery tests; these are not GPT-6 Pro behavior tests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from .api import handler
from .engine import Store, Engine, Conflict, parse_calls, verify_checks, digest
from .provider import find_final, ProviderFailure, WebModel, ASTRA


class FakeBridge:
    calls=[]
    def __init__(self,cwd):
        self.info={'tools':[dict(name=x,description=x,inputSchema={'type':'object','properties':{}}) for x in ('read','write','bash')]}
    def call(self,name,args,key):
        self.calls.append((name,args,key))
        if name=='bash_status':return {'structuredContent':{'status':'succeeded','result':{'exitCode':0}},'isError':False}
        if name=='bash':return {'structuredContent':{'status':'running'},'isError':False}
        return {'isError':False,'structuredContent':{'text':'beta\n'}}
    def close(self):pass


class Model:
    def __init__(self,responses):self.responses=iter(responses);self.sends=0;self.recoveries=0
    def send(self,task,prompt,save,cancelled):
        self.sends+=1;save(submission_started=True,conversation_id='00000000-0000-0000-0000-000000000000')
        return next(self.responses)
    def recover(self,task):self.recoveries+=1;return next(self.responses)


def answer(name,args):
    if name in ('read','write','bash'):name='vm_'+name
    return {'text':json.dumps({'tool_calls':[{'name':name,'arguments':args}]}),'message_id':'answer','model':'gpt-6-pro'}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'state')
        self.spec={'account':'c','cwd':str(self.root),'goal':'verify a file'};FakeBridge.calls=[]
    def tearDown(self):self.temp.cleanup()
    def create(self,key='test',**data):
        self.store.create(self.spec,key);self.store.save(key,phase='ready',**data);return self.store.get(key)
    def run_engine(self,model,key='test'):
        engine=Engine(self.store,model,FakeBridge);engine.active.add(key);engine._worker(key);return self.store.get(key)
    def test_idempotency_and_conflict(self):
        first,created=self.store.create(self.spec,'one');self.assertTrue(created)
        second,created=self.store.create(self.spec,'one');self.assertFalse(created);self.assertEqual(first,second)
        with self.assertRaises(Conflict):self.store.create({**self.spec,'goal':'changed'},'one')
    def test_reject_input_before_acceptance(self):
        for spec in ({**self.spec,'cwd':'relative'},{**self.spec,'account':'d'},{**self.spec,'model':'wrong'}):
            with self.assertRaises(ValueError):self.store.create(spec,'invalid')
        self.assertEqual(self.store.all(),[])
    def test_strict_protocol_never_partial_executes(self):
        for text in ('done','{"tool_calls":[{"name":"write","arguments":{}},{"name":"unknown","arguments":{}}]}','{"tool_calls":[]}'):
            with self.assertRaises(ValueError):parse_calls(text,{'write'})
    def test_file_checks_are_independent(self):
        (self.root/'probe').write_text('beta\n')
        results=verify_checks(self.root,[{'path':'probe','equals':'beta\n'},{'path':'probe','equals':'alpha'},{'path':'missing','equals':'x'},{'path':'gone','absent':True}])
        self.assertEqual([r['ok'] for r in results],[True,False,False,True])
    def test_fabricated_completion_is_rejected(self):
        self.create();m=Model([answer('finish',{'summary':'done','evidence':['invented']}),answer('needs_input',{'reason':'test ends','next_action':'inspect'})])
        task=self.run_engine(m);self.assertEqual(task['state'],'needs_input')
        self.assertTrue(next(iter(task['receipts'].values()))['isError'])
    def test_write_receipt_alone_cannot_verify_completion(self):
        self.create(receipts={'write-id':{'_tool':'write','isError':False}})
        m=Model([answer('finish',{'summary':'done','evidence':['write-id']}),answer('needs_input',{'reason':'test','next_action':'inspect'})])
        self.assertNotEqual(self.run_engine(m)['state'],'completed')
    def test_read_then_verified_completion(self):
        self.create();rid='agent-'+digest(['test',1,0])[:40]
        m=Model([answer('read',{'path':'probe'}),answer('finish',{'summary':'read','evidence':[rid]})])
        task=self.run_engine(m);self.assertEqual(task['state'],'completed');self.assertTrue(task['pending'][0]['started'] is False) # finish is controller-only
        self.assertEqual(len(FakeBridge.calls),1)
    def test_descriptive_evidence_and_expected_failure_are_accepted(self):
        self.create(receipts={'read-id':{'_tool':'read','isError':False},'failure-id':{'_tool':'bash','isError':True}})
        m=Model([answer('finish',{'summary':'passed; expected failure observed','evidence':['read-id: verified','failure-id: exit 7 expected']})])
        self.assertEqual(self.run_engine(m)['state'],'completed')
    def test_finish_can_follow_read_in_same_response(self):
        calls=parse_calls('{"tool_calls":[{"name":"read","arguments":{}},{"name":"finish","arguments":{"summary":"done","evidence":[]}}]}',{'read','finish'})
        self.assertEqual([x['name'] for x in calls],['read','finish'])
    def test_pending_bash_recovery_never_resubmits(self):
        self.create();self.store.save('test',phase='executing',pending=[dict(id='original',name='bash',arguments={'command':'write something'},started=True,done=False)])
        m=Model([answer('finish',{'summary':'done','evidence':['original']})]);task=self.run_engine(m)
        self.assertEqual(task['state'],'completed');self.assertEqual([c[0] for c in FakeBridge.calls],['bash_status'])
    def test_missing_original_bash_receipt_stays_unknown(self):
        self.create();self.store.save('test',phase='executing',pending=[dict(id='original',name='vm_bash',arguments={'command':'write something'},started=True,done=False)])
        with patch.object(FakeBridge,'call',return_value={'isError':True,'error':'not found'}):
            task=self.run_engine(Model([]))
        self.assertEqual(task['error']['stage'],'command_outcome_unknown')
    def test_transient_disconnect_reconnects_without_command_replay(self):
        self.create();self.store.save('test',phase='executing',pending=[dict(id='original',name='vm_bash',arguments={'command':'write something'},started=False,done=False)])
        with patch.object(FakeBridge,'call',side_effect=RuntimeError('connection lost')),patch('agent.engine.threading.Timer') as timer:
            task=self.run_engine(Model([]))
        self.assertEqual(task['state'],'queued');timer.assert_called_once()
        task=self.run_engine(Model([answer('finish',{'summary':'done','evidence':['original']})]))
        self.assertEqual(task['state'],'completed');self.assertEqual([c[0] for c in FakeBridge.calls],['bash_status'])
    def test_uncertain_write_is_not_replayed(self):
        self.create();self.store.save('test',phase='executing',pending=[dict(id='original',name='write',arguments={'path':'probe','content':'x'},started=True,done=False)])
        task=self.run_engine(Model([]));self.assertEqual(task['error']['stage'],'tool_outcome_unknown');self.assertEqual(FakeBridge.calls,[])
    def test_model_timeout_uses_history(self):
        self.create(conversation_id='00000000-0000-0000-0000-000000000000',submission_started=True,user_message_id='u')
        self.store.save('test',phase='waiting_model')
        m=Model([answer('needs_input',{'reason':'test','next_action':'inspect'})]);self.run_engine(m)
        self.assertEqual(m.sends,0);self.assertEqual(m.recoveries,1)
    def test_native_tool_route_is_not_duplicated(self):
        self.create();response=answer('write',{'path':'probe','content':'x'});response['native_tools']=['api_tool.call_tool']
        task=self.run_engine(Model([response]));self.assertEqual(task['error']['stage'],'unexpected_native_tool_route')
        self.assertEqual(FakeBridge.calls,[])
    def test_done_call_is_not_reexecuted(self):
        self.create(receipts={'original':{'_tool':'read','isError':False}})
        self.store.save('test',phase='executing',pending=[dict(id='original',name='read',arguments={},started=True,done=True)])
        task=self.run_engine(Model([answer('finish',{'summary':'done','evidence':['original']})]))
        self.assertEqual(task['state'],'completed');self.assertEqual(FakeBridge.calls,[])
    def test_final_requires_exact_user_branch_and_actual_model(self):
        msg={'id':'a','author':{'role':'assistant'},'channel':'final','status':'finished_successfully','metadata':{'model_slug':'gpt-6-pro'},'content':{'parts':['done']}}
        history={'mapping':{'u':{'message':{'author':{'role':'user'}}},'a':{'parent':'u','message':msg}}}
        self.assertEqual(find_final(history,'u')['text'],'done');self.assertIsNone(find_final(history,'other'))
        msg['metadata']['model_slug']='wrong'
        with self.assertRaises(ProviderFailure):find_final(history,'u')
    def test_observed_astra_alias_keeps_actual_identity_and_effort(self):
        msg={'id':'a','author':{'role':'assistant'},'channel':'final','status':'finished_successfully',
             'metadata':{'resolved_model_slug':'gpt-5-6-auto-thinking','default_model_slug':ASTRA,'thinking_effort':'max'},'content':{'parts':['done']}}
        history={'mapping':{'u':{'message':{'author':{'role':'user'}}},'a':{'parent':'u','message':msg}}}
        result=find_final(history,'u',ASTRA)
        self.assertEqual(result['model'],'gpt-5-6-auto-thinking');self.assertEqual(result['thinking_effort'],'max')
        with self.assertRaises(ProviderFailure):find_final(history,'u','gpt-6-pro')
    def test_complete_stream_without_answer_is_not_running_forever(self):
        task=dict(account='b',model='gpt-6-pro',conversation_id='00000000-0000-0000-0000-000000000000',user_message_id='u')
        with patch('agent.provider.auth_for'),patch('agent.provider.get_json',side_effect=[{'mapping':{}},{'status':'COMPLETE'},{'mapping':{}}]):
            with self.assertRaises(ProviderFailure) as error:WebModel().recover(task)
        self.assertEqual(error.exception.details['stage'],'model_result_missing')
    def test_cancel_before_preflight_never_calls_model_or_account(self):
        self.store.create(self.spec,'test');self.store.save('test',cancelled=True,state='cancel_requested')
        with patch('agent.engine.account_status') as account:
            task=self.run_engine(Model([]))
        self.assertEqual(task['state'],'cancelled');account.assert_not_called()
    def test_http_auth_and_bad_request(self):
        engine=Engine(self.store,Model([]),FakeBridge)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.store,engine,'test-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+'/v1/tasks')
            self.assertEqual(error.exception.code,401)
            error.exception.close()
            request=urllib.request.Request(url+'/v1/tasks',headers={'Authorization':'Bearer test-token'},data=b'{}')
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,400);self.assertEqual(self.store.all(),[])
            error.exception.close()
            request=urllib.request.Request(url+'/v1/tasks',headers={'Authorization':'Bearer test-token','Origin':'https://unrelated.invalid'},data=b'{}')
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,403);error.exception.close();self.assertEqual(self.store.all(),[])
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
