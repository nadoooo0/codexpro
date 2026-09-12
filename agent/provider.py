"""Use the pinned chatgpt-api Web transport; never invoke a paid model API.

Only final answers and request identities are retained. Existing account captures
are read, never changed. Each task owns a new conversation, outside research slots.
"""
import asyncio
import json
import os
from pathlib import Path
import re
from chatgpt_api.core.types import ChatRequest, Message
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport
from curl_cffi import requests

UPSTREAM = 'https://github.com/suphotP/chatgpt-api'
UPSTREAM_COMMIT = 'f998a6d83f324cb3187396dd7efced0c40f29601'
MODEL = 'gpt-6-pro'
ASTRA = 'gpt-6-astra-wm'
MODELS = (MODEL, ASTRA)
CAPTURES = Path(os.environ.get('AGENT_ACCOUNTS_DIR', str(Path.home()/'.local/state/labor-federation/accounts')))


class ProviderFailure(Exception):
    def __init__(self, stage, status=None, kind=None):
        self.details = dict(stage=stage, http_status=status, error_type=kind,
                            generation_replayed=False)
        super().__init__(json.dumps(self.details))


def auth_for(account):
    path = CAPTURES/account/'session.json'
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ProviderFailure('account_capture', kind='MissingProtectedSession')
    data = json.loads(path.read_text())
    if not data.get('access_token'):
        raise ProviderFailure('account_capture', status=401)
    return ChatGPTAuthConfig(access_token=data['access_token'], cookies=data.get('cookies', {}),
                            headers=data.get('headers', {}), captured_url='https://chatgpt.com/backend-api/f/conversation',
                            captured_request_json=data.get('request_template', {}), source=data.get('source'))


def get_json(auth, path):
    try:
        response = requests.get('https://chatgpt.com/backend-api/'+path,
                                headers=auth.request_headers(), timeout=30, impersonate='chrome')
        if response.status_code != 200:
            raise ProviderFailure(path.split('/')[0], response.status_code)
        return response.json()
    except ProviderFailure:
        raise
    except Exception as exc:
        raise ProviderFailure(path.split('/')[0], kind=type(exc).__name__) from None


def account_status(account, model=MODEL):
    authenticated=None
    try:
        data = get_json(auth_for(account), 'models')
        authenticated=True
        models, efforts = set(), set()
        def walk(value, active=False):
            if isinstance(value, dict):
                slug = value.get('slug', value.get('model_slug'))
                if slug: active = slug == model; models.add(slug)
                for key, child in value.items():
                    if active and 'effort' in key and isinstance(child, list):
                        for entry in child:
                            if isinstance(entry, str): efforts.add(entry)
                            elif isinstance(entry, dict):
                                for k in ('value', 'id', 'slug', 'effort', 'thinking_effort'):
                                    if isinstance(entry.get(k), str): efforts.add(entry[k])
                    walk(child, active)
            elif isinstance(value, list):
                for child in value: walk(child, active)
        walk(data)
        info=ChatGPTWebTransport(auth_for(account),timeout=30,impersonate='chrome').conversation_init(model)
        limits=[x for x in info.get('model_limits',[]) if x.get('model_slug')==model]
        return dict(account=account, authenticated=True, model_available=model in models and not limits,
                    model=model, efforts=sorted(efforts),model_limits=limits)
    except ProviderFailure as exc:
        return dict(account=account, authenticated=False if exc.details.get('http_status')==401 else authenticated, **exc.details)
    except Exception as exc:
        return dict(account=account,authenticated=authenticated,stage='model_availability',error_type=type(exc).__name__)


def find_final(history, user_id, expected_model=MODEL):
    mapping = history.get('mapping', {})
    for node in reversed(list(mapping.values())):
        msg = node.get('message') or {}
        if msg.get('author', {}).get('role') != 'assistant' or msg.get('channel') != 'final':
            continue
        if msg.get('status') != 'finished_successfully': continue
        ancestor, visited, native_tools = node.get('parent'), set(), []
        while ancestor and ancestor not in visited:
            if ancestor == user_id:
                meta = msg.get('metadata', {})
                actual = meta.get('resolved_model_slug') or meta.get('model_slug')
                # Observed web alias: the Astra selector returns these internal
                # thinking IDs. Preserve all identities; never apply this to Pro.
                selected=meta.get('default_model_slug')
                astra_alias=(expected_model==ASTRA and selected==ASTRA and
                             actual in ('gpt-5-6-auto-thinking','gpt-5-6-thinking'))
                if actual != expected_model and not astra_alias:
                    raise ProviderFailure('model_identity', kind='UnexpectedOrMissingModel')
                text = '\n'.join(x for x in msg.get('content', {}).get('parts', []) if isinstance(x, str))
                return dict(text=text, message_id=msg['id'], model=actual,selected_model=selected,
                            requested_model=expected_model,thinking_effort=meta.get('thinking_effort'),native_tools=native_tools)
            visited.add(ancestor)
            parent = mapping.get(ancestor, {})
            author=(parent.get('message') or {}).get('author',{})
            if author.get('role')=='tool':native_tools.append(author.get('name','unknown'))
            if (parent.get('message') or {}).get('author', {}).get('role') == 'user':
                break  # Never borrow a later answer from another user turn.
            ancestor = parent.get('parent')
    return None


class WebModel:
    def recover(self, task):
        cid = task.get('conversation_id')
        if not cid: return None
        if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', cid):
            raise ProviderFailure('conversation_identity', kind='InvalidConversation')
        auth=auth_for(task['account'])
        history = get_json(auth, 'conversation/'+cid)
        answer=find_final(history, task['user_message_id'],task['model'])
        if answer:return answer
        status=get_json(auth,'conversation/'+cid+'/stream_status')
        if status.get('status')=='COMPLETE':
            # Re-read after COMPLETE so a race with final persistence isn't failure.
            history=get_json(auth,'conversation/'+cid)
            answer=find_final(history, task['user_message_id'],task['model'])
            if answer:return answer
            raise ProviderFailure('model_result_missing',kind='StreamCompleteWithoutMatchingFinal')
        return None

    def send(self, task, prompt, save, cancelled):
        auth = auth_for(task['account'])
        class Observed(ChatGPTWebTransport):
            def _build_chat_payload_with_uploaded_media(self, request, headers):
                payload = super()._build_chat_payload_with_uploaded_media(request, headers)
                payload['messages'][-1]['id'] = task['user_message_id']
                payload['parent_message_id'] = task['parent_message_id']
                if task.get('conversation_id'): payload['conversation_id'] = task['conversation_id']
                payload['history_and_training_disabled'] = False
                return payload

            def _iter_post_conversation(self, url, headers, payload):
                if cancelled(): raise ProviderFailure('before_submission', kind='Cancelled')
                save(phase='model_submitting', state='running', submission_started=True)
                for event in super()._iter_post_conversation(url, headers, payload):
                    yield event

        # Transport timeout is only an observation limit: history recovery follows.
        transport = Observed(auth, timeout=180, impersonate='chrome')
        async def run():
            request = ChatRequest(messages=[Message.text('user', prompt)], model=task['model'],
                                  conversation_id=task.get('conversation_id'),
                                  parent_message_id=task['parent_message_id'],
                                  thinking_effort=task.get('effort'),
                                  metadata={'history_and_training_disabled':False})
            async for delta in transport.stream_chat(request):
                def identity(v):
                    if isinstance(v, dict):
                        cid = v.get('conversation_id')
                        if isinstance(cid, str) and cid != task.get('conversation_id'):
                            save(conversation_id=cid)
                        for x in v.values(): identity(x)
                    elif isinstance(v, list):
                        for x in v: identity(x)
                identity(delta.raw)
                def error_code(v):
                    if isinstance(v,dict):
                        code=v.get('error_code')
                        if isinstance(code,str):return code
                        if isinstance(v.get('error'),dict):
                            code=v['error'].get('code') or v['error'].get('type')
                            if isinstance(code,str):return code
                        for x in v.values():
                            found=error_code(x)
                            if found:return found
                    elif isinstance(v,list):
                        for x in v:
                            found=error_code(x)
                            if found:return found
                    return None
                code=error_code(delta.raw)
                if code:
                    safe=code if re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',code) else 'ProviderError'
                    raise ProviderFailure('generation_rejected',kind=safe)
        try:
            asyncio.run(run())
        except ProviderFailure:
            raise
        except Exception as exc:
            match = re.search(r'(?:failed:|HTTP\s+)\s*(401|403|429|5\d\d)\b', str(exc))
            raise ProviderFailure('generation_transport', int(match[1]) if match else None,
                                  type(exc).__name__) from None
        return self.recover(task)
