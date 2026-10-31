"""Operator-only terminal transport; not an external IM or IAM implementation."""
import copy
import hashlib
import json
import threading

from channel_contracts import UnifiedMessage


class LocalChannelAdapter:
    channel_name = 'local_demo'

    def __init__(self, gateway, identities, max_events=1024):
        if not isinstance(max_events, int) or max_events < 1:
            raise ValueError('Invalid event limit')
        self.gateway = gateway
        # Provisioned by trusted local operator setup, never by message fields.
        self.identities = copy.deepcopy(identities)
        self.max_events = max_events
        self._events = {}
        self._lock = threading.Lock()

    def parse(self, raw):
        return UnifiedMessage(channel=self.channel_name, user_id=raw['user_id'],
            conversation_id=raw['conversation_id'], text=raw['text'],
            message_id=raw['message_id'], metadata={'device_id': raw['device_id']})

    def send(self, conversation_id, payload):
        # A local terminal receipt, never a claim that a platform delivered it.
        return {'delivery': 'LOCAL_DEMO_ONLY', 'conversation_id': conversation_id,
                'notice': '本地终端演示回执；未连接外部平台；身份为受信本地演示配置。',
                'response': copy.deepcopy(payload)}

    def receive(self, session, event):
        identity = self.identities.get(session) if isinstance(session, str) else None
        if not identity:
            raise PermissionError('Unknown local operator session')
        allowed = {'message_id', 'conversation_id', 'text', 'action', 'plan_id', 'action_token', 'credential_id', 'code'}
        if not isinstance(event, dict) or set(event) - allowed:
            raise ValueError('Unsupported fields; identity is provisioned by the operator')
        if any(not isinstance(v, str) or len(v) > 2000 for v in event.values()):
            raise ValueError('Only bounded text fields are supported')
        if any(not event.get(k, '').strip() or len(event[k]) > 128 for k in ('message_id', 'conversation_id')):
            raise ValueError('message_id and conversation_id are required (1..128 characters)')
        action = event.get('action', 'chat')
        if action not in {'chat', 'confirm', 'verify', 'cancel'}:
            raise ValueError('Unsupported action')
        if action == 'chat' and (not event.get('text', '').strip() or set(event) - {'message_id', 'conversation_id', 'text', 'action'}):
            raise ValueError('A chat event requires text only')
        if action != 'chat' and (not event.get('plan_id') or not event.get('action_token') or 'text' in event):
            raise ValueError('An explicit plan and action token are required')
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=True).encode('ascii')).hexdigest()
        key = (session, event['message_id'])
        # Serialize ingress and cache even an uncertain failure; no auto retry of writes.
        with self._lock:
            cached = self._events.get(key)
            if cached:
                if cached[0] != fingerprint:
                    raise ValueError('message_id reused with different content')
                return copy.deepcopy(cached[1])
            if len(self._events) >= self.max_events:
                raise ValueError('Local event capacity reached; no events were executed')
            actor, device = identity['user_id'], identity['device_id']
            try:
                if action == 'chat':
                    response = self.gateway.handle(self.parse(dict(event, user_id=actor, device_id=device)))
                elif action == 'confirm':
                    response = self.gateway.runtime.confirm(event['plan_id'], event['action_token'],
                        event.get('credential_id', ''), device, actor)
                elif action == 'verify':
                    response = self.gateway.runtime.verify(event['plan_id'], event['action_token'],
                        event.get('code', ''), event.get('credential_id', ''), device, actor)
                else:
                    response = self.gateway.runtime.cancel(event['plan_id'], event['action_token'], actor, device)
            except Exception:
                response = {'ok': False, 'error_code': 'LOCAL_RESULT_UNCERTAIN',
                            'message': '本地处理结果暂不能确认；不会自动重试，请核验原计划与审计。'}
            receipt = self.send(event['conversation_id'], response)
            self._events[key] = (fingerprint, copy.deepcopy(receipt))
            return receipt
