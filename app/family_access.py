"""C4 trusted family adapter. Demo bearer fixtures MUST be replaced by real IAM."""
import hmac
import os
import uuid
from types import SimpleNamespace


class FamilyAccess:
    def __init__(self, runtime):
        self.rt = runtime
        self.store = runtime.bank.guardianships
        # Offline identity fixtures, exactly like C4's demo bearer sessions.
        # Production replacement: verified IAM session -> server-side user/device/role.
        # Never provision identities or roles from an HTTP body or an LLM response.
        self.demo_sessions = {
            'demo-user-session': {'user_id': 'demo-user', 'device_id': 'demo-device', 'role': 'user'},
        }
        # No shipped admin credential and no admin token in the browser bundle.
        admin_token = os.getenv('BANK_AGENT_DEMO_ADMIN_TOKEN', '').strip()
        reserved = set(self.demo_sessions) | {a['demo_session'] for a in self.store.accounts.values()}
        if admin_token and admin_token not in reserved:
            self.demo_sessions[admin_token] = {'user_id': 'demo-operator', 'device_id': 'operator-device', 'role': 'admin'}

    def deny(self, reason, plan=None, actor='', subject='', device=''):
        trace = plan.trace_id if plan else 'trace_family_' + uuid.uuid4().hex
        context = plan.entities.get('_family', {}) if plan else {}
        self.rt.audit.write(trace, 'authz.denied', {
            'user_id': plan.actor_id if plan else subject or actor or 'unknown-user',
            'device_id': device or 'unknown-device',
            'task_id': plan.plan_id if plan else '', 'guardian_id': context.get('guardian_id', actor),
            'subject_id': plan.actor_id if plan else subject, 'decision_reason': reason,
        }, actor=actor or 'anonymous', risk_level='red',
            intent=plan.intent if plan else 'family_access')
        return {'ok': False, 'blocked': True, 'error_code': 'AUTHZ_DENIED', 'trace_id': trace, 'message': reason}

    def chat(self, text, guardian_id, device_id, subject_id, mode='delegate'):
        guardian = self.store.accounts.get(guardian_id)
        owner = self.store.accounts.get(subject_id)
        if not guardian or not owner or self.store.device_for(guardian_id) != device_id or not self.store.device_for(subject_id):
            return self.deny('家庭身份或绑定设备无效', actor=guardian_id, subject=subject_id)
        grant = self.store.between(guardian_id, subject_id)
        service=getattr(self.rt,'identity_service',None)
        if service:mode='assist'
        context = dict(guardian_id=guardian_id, guardian_device_id=device_id, subject_id=subject_id,
                       grant_id=grant.grant_id if grant else '', mode=mode,consent_version=self.store.consent(grant.grant_id)['version'] if grant else 0)
        response=self.rt.chat(text, actor_id=subject_id, device_id=self.store.device_for(subject_id), family_context=context)
        if service and response.get('plan_view') and response.get('plan',{}).get('requires_confirmation'):
            response['plan_view'].update(requires_confirmation=False,action_token='',confirmation_hint='请转交账户本人核对')
            response['message']='已生成家庭协助草稿，请将任务编号交由账户本人核对；尚未转账。'
        return response

    def set_permissions(self,grant_id,version,permissions,code):
        service=getattr(self.rt,'identity_service',None)
        grant=self.store.get(grant_id);identity=service.current_identity() if service else None
        if not identity or not grant or identity['user_id']!=grant.subject_id or identity['role']!='user':return self.deny('仅账户本人可核准家庭权限')
        if not isinstance(permissions,dict) or set(permissions)!={'read','notify','assist'} or any(type(v) is not bool for v in permissions.values()) or permissions['assist'] and not permissions['read']:return {'ok':False,'message':'协助起草需要明确阅读授权；三项权限分别勾选。'}
        if type(version) is not int or version!=self.store.consent(grant_id)['version']:return {'ok':False,'message':'权限版本已变化，请刷新后重新核对。'}
        if not service.approve_family_scope(grant_id,permissions,version,code):return self.deny('本人当前未使用的动态码未通过')
        try:consent=self.store.set_consent(grant_id,identity['user_id'],version,permissions)
        except (ValueError,PermissionError):return {'ok':False,'message':'权限未修改，请核对关系有效期和版本。'}
        self.rt.audit.write('family_scope_'+uuid.uuid4().hex,'family.scope.updated',{'grant_id':grant_id,'version':consent['version'],'permissions':permissions},actor=identity['user_id'],intent='family_authorization')
        return {'ok':True,'consent':consent,'message':'仅本机虚构家庭账户授权已保存；协助不会代替本人认证或提交。'}

    def permission_records(self,actor):
        with self.store.lock:rows=self.store.db.execute('SELECT grant_id,guardian_id,subject_id FROM guardianships WHERE guardian_id=? OR subject_id=?',(actor,actor)).fetchall()
        return [dict(row,consent=self.store.consent(row['grant_id']),is_owner=row['subject_id']==actor) for row in rows]

    def review(self,task_id):
        service=getattr(self.rt,'identity_service',None);identity=service.current_identity() if service else None
        with self.rt.recovery_task_lock(task_id):
            plan=self.rt.pending.get(task_id)
            if not identity or not plan or not plan.entities.get('_family') or plan.actor_id!=identity['user_id'] or plan.device_id!=identity['device_id']:return self.deny('仅绑定设备上的账户本人可核对该协助草稿')
            denied=self.check_identity(plan,identity['user_id'],identity['device_id'],'verify')
            if denied:return denied
            decision=self.rt.policy.classify(plan.intent,plan.entities,plan.actor_id)
            if decision.get('authz_allowed') is False:return self.deny(decision['decision_reason'],plan)
            return self.rt._response(plan,'这是亲友协助起草的计划；请本人核对，并完成新的强认证和最终确认。')

    def check_identity(self, plan, actor, device, action):
        service=getattr(self.rt,'identity_service',None)
        if service:
            current=service.current_identity()
            if not current or current['role']!='user' or current['user_id']!=plan.actor_id or current['device_id']!=plan.device_id:
                return self.deny('当前登录主体或设备不能操作此任务',plan,actor,device=device)
        ctx = plan.entities.get('_family')
        if not ctx:
            if plan.actor_id in self.store.accounts and (actor != plan.actor_id or device != plan.device_id):
                return self.deny('必须使用本人绑定身份和设备', plan, actor, device=device)
            return None
        owner = actor == plan.actor_id and device == plan.device_id
        guardian = actor == ctx['guardian_id'] and device == ctx['guardian_device_id']
        if not owner and (not guardian or action == 'verify' or (action == 'confirm' and ctx['mode'] == 'assist')):
            return self.deny('此步骤必须被守护人本人操作；不能替代本人认证或大额确认', plan, actor, device=device)
        return None

    def http_context(self, authorization, body, required=False, action=''):
        if any(k in body and not isinstance(body[k], str) for k in ('user_id', 'device_id', 'subject_id', 'plan_id', 'task_id')):
            return self.deny('身份和任务标识必须是字符串')
        token = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else ''
        service=getattr(self.rt,'identity_service',None)
        account = service.current_identity() if service else next((dict(a, family=True, role='user') for a in self.store.accounts.values()
                        if token and hmac.compare_digest(a['demo_session'].encode(), token.encode())), None)
        if account is None and not service:
            account = next((dict(a, family=False) for key, a in self.demo_sessions.items()
                            if token and hmac.compare_digest(key.encode(), token.encode())), None)
        task_id = body.get('plan_id') or body.get('task_id')
        plan = self.rt.pending.get(task_id) or self.rt.auth_plan_snapshots.get(task_id)
        # Resource ownership remains enforceable after completion and process restart.
        if task_id and plan is None:
            saved = self.rt.recovery_store.get(task_id)
            if saved:
                plan = SimpleNamespace(**saved[1]['plan'])
        required = required or body.get('user_id') in self.store.accounts or body.get('subject_id') in self.store.accounts
        required = required or bool(plan and (plan.entities.get('_family') or plan.actor_id in self.store.accounts))
        actor = account['user_id'] if account else ''
        if not account:
            if required or authorization:
                return self.deny('请使用可信登录会话，不能通过请求体声明身份', plan)
            return {'ok': True, 'managed': False}
        if any(body.get(key) not in (None, '', account[key]) for key in ('user_id', 'device_id')):
            return self.deny('请求身份与登录会话不一致', plan, actor=actor, device=account['device_id'])
        if plan:
            if plan.entities.get('_family'):
                denied = self.check_identity(plan, actor, account['device_id'], action)
                if denied:
                    return denied
            elif actor != plan.actor_id or account['device_id'] != plan.device_id:
                return self.deny('当前会话不能操作其他用户或设备的任务', plan, actor=actor, device=account['device_id'])
        return {'ok': True, 'managed': True, 'family': account.get('family', False),
                'role': account.get('role', 'user'), 'user_id': actor, 'device_id': account['device_id']}

    def resource_guard(self, context, owner):
        if context.get('user_id') != owner:
            return self.deny('此查询仅向账户本人开放；守护人请使用家庭授权查询入口', actor=context.get('user_id', ''), subject=owner)
        return None

    def trace_guard(self, context, trace_id):
        events = self.rt.audit.list_for_trace(trace_id)
        if events:
            # A denied request can mention an attacker. The first event owns the trace.
            return self.resource_guard(context, events[0]['user_id'])
        return None

    def visible_audit(self, context):
        items = self.rt.audit.list()
        traces = {}
        for item in items:
            trace = item['trace_id']
            if trace not in traces:
                events = self.rt.audit.list_for_trace(trace)
                traces[trace] = bool(events and events[0]['user_id'] == context.get('user_id'))
        return [item for item in items if traces[item['trace_id']]]

    def guardian_inbox(self, guardian):
        items = []
        for grant in self.store.grants_for(guardian):
            if grant['active'] and (not self.store.require_explicit_consent or self.store.consent(grant['grant_id'])['notify']):
                items.extend(n for n in self.rt.notifications.list_notifications(grant['subject_id'], channel='guardian')
                             if n['target'] == guardian and n['status'] == 'LOCAL_QUEUED')
        if self.store.require_explicit_consent:
            items=[{k:v for k,v in item.items() if k in {'notification_id','event_type','status','created_at'}}|{'message':'账户风险提醒，请联系本人核对；未共享交易明细。'} for item in items]
        return items
