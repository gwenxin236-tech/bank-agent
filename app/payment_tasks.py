"""Reprepare only unsubmitted native payment tasks; never submit or retry here."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone
import sqlite3
from action_effects import normalize_constraints,build_transfer_contract
from task_dag import plan_to_dag,DagExecutor


def payment_policy(policy,entities):
    constraints=normalize_constraints(entities.get('_business_constraints',{}))
    value=dict(policy)
    value['preflight_security_buffer_cents']=max(policy['preflight_security_buffer_cents'],constraints.get('reserve_cents',0))
    return value


def resume_payment(runtime,task_id,version,changes,actor_id,device_id,*,policy,registry,query_only=False):
    def fail(code,message,status=409):return {'ok':False,'error_code':code,'message':message,'http_status':status}
    if not isinstance(task_id,str) or not 1<=len(task_id)<=128:return fail('INVALID_TASK','任务编号不合法。',400)
    if runtime.recovery_failed:return runtime.recovery_unavailable()
    with runtime.recovery_task_lock(task_id):
        identity=runtime.identity_service.current_identity() if runtime.identity_service else None
        if runtime.identity_service and (not identity or identity['role']!='user' or identity['user_id']!=actor_id or identity['device_id']!=device_id or actor_id!='demo-user'):
            return fail('TASK_ACCESS_DENIED','当前身份不能办理此任务。',403)
        task=runtime.business_tasks.get(task_id,actor_id,device_id)
        entry=runtime.recovery_store.get(task_id)
        if not task or not entry:return fail('TASK_NOT_FOUND','当前会话无法查看该任务。',404)
        state,payload=entry;raw=payload['plan']
        if raw['actor_id']!=actor_id or raw['device_id']!=device_id or raw['intent']!='transfer' or raw['entities'].get('_family') or actor_id in runtime.bank.guardianships.accounts:
            return fail('TASK_SCOPE_DENIED','本入口仅支持账户本人未提交的单笔支付任务。',403)
        if type(version) is not int or version!=task['version']:return fail('TASK_VERSION_CONFLICT','任务已变化，请刷新记录后重新核对。')
        if not isinstance(changes,dict) or set(changes)-{'amount_cents','beneficiary','note','deadline','reserve_cents'}:
            return fail('INVALID_CHANGES','只可修改金额、收款人、备注、最晚提交时间和保留金额。',400)
        if query_only or state in {'processing','unknown','recovery_required'}:
            if changes:return fail('QUERY_ONLY','该任务只能查询原请求，不能修改或重复提交。')
            runtime._reconcile_recovery_task(task_id)
            return {'ok':True,'query_only':True,'business_executed':False,'business_task':runtime.business_tasks.get(task_id,actor_id,device_id),
                    'message':'已核对原请求记录；结果未知不代表失败，不会重新转账。'}
        if state not in {'blocked','needs_clarification','waiting_confirmation','expired'}:
            return fail('TASK_NOT_RESUMABLE','任务已提交、终止或受保护，请查询原结果或使用原任务控制。')
        dag=runtime.dag_store.get('plan-'+task_id) if runtime.dag_store else None
        if dag and (dag.metadata.get('source')!='AgentPlan' or dag.status not in {'running','waiting_human'} or
                    any(n.node_type not in {'action','human_gate'} or
                        (n.node_type=='action' and n.status!='pending') or
                        (n.node_type=='human_gate' and (n.status not in {'pending','running'} or n.node_id!='confirmation')) for n in dag.nodes)):
            return fail('USE_DAG_CONTROL','任务含已执行步骤或受控关口，请使用原任务控制，不可重新准备覆盖。')
        if any(s['status']!='pending' for s in raw['steps']):return fail('TASK_ALREADY_STARTED','已有执行记录，不能重新准备。')
        if runtime.breaker.is_locked():return fail('CIRCUIT_BREAKER_LOCKED','安全熔断已触发，请联系人工。',403)
        entities={k:deepcopy(v) for k,v in raw['entities'].items() if not k.startswith('_') and k not in {'beneficiary_account','raw'}}
        if 'amount_cents' in changes:
            amount=changes['amount_cents']
            if type(amount) is not int or not 0<amount<=2**63-1:return fail('INVALID_AMOUNT','金额必须是正整数分。',400)
            entities['amount_cents']=amount;entities.pop('amount_error',None)
        for field,limit in (('beneficiary',80),('note',500)):
            if field in changes:
                text=changes[field]
                if not isinstance(text,str) or len(text)>limit or any(ord(c)<32 for c in text) or (field=='beneficiary' and not text.strip()):
                    return fail('INVALID_CHANGES','收款人或备注格式不合法。',400)
                entities[field]=text.strip()
                if field=='beneficiary':entities.pop('account_requires_verification',None)
        constraints=dict(raw['entities'].get('_business_constraints',{}))
        constraints.update({k:changes[k] for k in ('deadline','reserve_cents') if k in changes})
        try:entities['_business_constraints']=normalize_constraints(constraints)
        except (ValueError,TypeError,OverflowError):return fail('INVALID_CONSTRAINTS','请使用整数分和含时区的最晚提交时间。',400)
        # Reuse server templates/PolicyEngine; no model, prose parser or caller-provided tools.
        plan=runtime.planner.build('',actor_id=actor_id,device_id=device_id,trace_id=raw['trace_id'],_trusted_transfer_entities=entities)
        plan.plan_id=task_id
        with runtime.bank.lock:
            plan.entities['_effect_contract']=build_transfer_contract(task_id,actor_id,device_id,plan.entities,plan.steps,plan.token_expires_at,
                policy,runtime.bank,registry)
        plan.entity_digest=runtime._auth_digest(plan)
        status='waiting_confirmation'
        if plan.risk_assessment.get('authz_allowed') is False or runtime.is_user_frozen(actor_id):
            response=runtime._response(plan,'账户授权或安全状态需要人工核对。',blocked=True);response['ok']=False;status='blocked'
        elif plan.risk_assessment.get('defer'):
            response=runtime._response(plan,'反诈保护正在生效，请走原保护性流程或联系人工。',blocked=True);response['ok']=False;status='blocked'
        elif runtime._invalid_input(plan):
            response=runtime._transfer_details(plan,'请补齐收款人和合法金额，再重新核对。');status='needs_clarification'
        else:
            response=runtime._prepare_payment_review(plan,'重新核对已保存支付任务','task-resume:'+task_id,'web',None)
            if response is not None:status='blocked'
            elif plan.entities.get('account_requires_verification') or plan.entities.get('beneficiary') not in runtime.bank.beneficiaries:
                response=runtime._transfer_details(plan,'收款账户尚未核验，请选择已核验的演示账户。');status='needs_clarification'
            else:response=runtime._response(plan,'任务已重新核对；请核对本次范围后本人确认，尚未转账。')
        deadline=constraints.get('deadline')
        if deadline and datetime.now(timezone.utc)>=datetime.fromisoformat(deadline):
            response=runtime._response(plan,'最晚提交时间已过，请明确修改时间后重新核对；未办理转账。',blocked=True)
            response.update(ok=False,error_code='BUSINESS_DEADLINE_EXPIRED');status='blocked'
        credentials=deepcopy(payload.get('credentials',[]))
        for credential in credentials:credential['invalidated_reason']='task_reprepared'
        new_payload={'plan':asdict(plan),'credentials':credentials}
        new_dag=None
        if status=='waiting_confirmation':
            new_dag=plan_to_dag(plan);new_dag.metadata['execution_owner']='dag'
            DagExecutor(new_dag,lambda *a: (_ for _ in ()).throw(ValueError('No dispatch during prepare')),lambda *a:None).run()
        try:
            with runtime.audit.lock,runtime.audit.db:
                if runtime.dag_store and new_dag:runtime.dag_store._write(new_dag,[{'event_type':'task.reprepared','previous_version':version}],commit=False)
                runtime.audit.write(plan.trace_id,'payment.task.reprepared',{'task_id':task_id,'previous_version':version,'requires_fresh_confirmation':True,'business_executed':False},actor=actor_id,intent='transfer',commit=False)
                runtime.recovery_store.save(task_id,status,new_payload)
        except (sqlite3.Error,OSError):
            runtime.recovery_failed=True;runtime.pending.pop(task_id,None);return runtime.recovery_unavailable()
        for credential in list(runtime.auth_credentials._items.values()):
            if credential.binding.task_id==task_id:credential.invalidated_reason='task_reprepared'
        runtime.consumed_tokens.add(raw['consent_token']);runtime.authenticated.discard(task_id);runtime.authenticated_credential.pop(task_id,None)
        with runtime.lock:
            runtime.pending.pop(task_id,None)
            if status=='waiting_confirmation':runtime.pending[task_id]=plan
        runtime.auth_plan_snapshots[task_id]=plan
        response.update(business_task=runtime.business_tasks.response(response,'reprepared'),business_executed=False,reprepared=True)
        return response
