"""Durable business projections in the existing workflow DB; never an executor."""
from datetime import datetime,timezone
import json,re
from action_effects import public_effect_summary

_STATES={'waiting_confirmation','processing','unknown','recovery_required','succeeded','failed','reversed','paused','cancelled','deferred','expired','blocked','needs_clarification','handoff'}
_FACT_FIELDS={'amount_cents','available_cents','current_cents','limit_cents','projected_cents','verified','directory_match','new_recipient','account_requires_verification','security_buffer_cents','confirmed_expenses_cents','disposable_cents','projected_disposable_cents','shortfall_cents'}

def _text(value,limit=256):
    if not isinstance(value,str):return ''
    value=re.sub(r'[\x00-\x1f\x7f]','',value)[:limit]
    value=re.sub(r'(?i)sk-[a-z0-9_-]{10,}|gh[pousr]_[a-z0-9_]{12,}|Bearer\s+\S+','[REDACTED]',value)
    return re.sub(r'\d{6,}',lambda m:'*'*min(len(m[0]),16),value)

def _now():return datetime.now(timezone.utc).isoformat()

class BusinessTaskStore:
    def __init__(self,audit):
        self.audit=audit
        with audit.lock,audit.db:
            audit.db.execute('''CREATE TABLE IF NOT EXISTS business_tasks (
                task_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,device_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            audit.db.execute('''CREATE TABLE IF NOT EXISTS business_task_events (
                task_id TEXT NOT NULL,seq INTEGER NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,
                timestamp TEXT NOT NULL,PRIMARY KEY(task_id,seq))''')
            audit.db.execute('CREATE INDEX IF NOT EXISTS business_tasks_owner ON business_tasks(actor_id,device_id,updated_at)')

    def checkpoint_locked(self,task_id,status,payload):
        """Called inside RecoveryStore.save's transaction: checkpoint/header commit together."""
        plan=payload.get('plan',{});entities=plan.get('entities',{})
        if not plan or plan.get('intent') not in {'transfer','fraud_guard'}:return
        if plan.get('plan_id')!=task_id or status not in _STATES:raise ValueError('Invalid task projection')
        amount=entities.get('amount_cents')
        if amount is not None and type(amount) is not int:raise ValueError('Integer cents required')
        checks=plan.get('preflight',{}).get('checks',{});generated=plan.get('preflight',{}).get('generated_at','')
        facts=[]
        for key,check in checks.items():
            evidence={k:v for k,v in check.get('evidence',{}).items() if k in _FACT_FIELDS and type(v) in (int,bool,type(None))}
            facts.append({'check':key,'status':check.get('status','unknown'),'evidence':evidence,
                          'source':'preflight.py:build_payment_preflight#'+key,'observed_at':generated,'coverage':'current_request_only'})
        recovery=plan.get('coordination',{}).get('payment_recovery',{})
        if status=='waiting_confirmation' and recovery.get('status') not in {None,'ready'}:status='blocked'
        budget=checks.get('budget',{}).get('evidence',{})
        constraints={k:budget[k] for k in ('security_buffer_cents','confirmed_expenses_cents') if type(budget.get(k)) is int}
        constraints.update(entities.get('_business_constraints',{}))
        value={'task_id':task_id,'plan_id':task_id,'trace_id':plan['trace_id'],'intent':plan['intent'],
            'actor_id':plan['actor_id'],'subject_id':plan['actor_id'],'device_id':plan['device_id'],
            'goal':{'amount_cents':amount,'beneficiary':_text(entities.get('beneficiary')),'currency':'CNY','note':_text(entities.get('note'),500)},
            'constraints':constraints,'facts':facts,'effect_summary':public_effect_summary(entities.get('_effect_contract')),
            'authorization':{'meaning':'requirements_and_records_only','confirmation_required':bool(plan.get('requires_confirmation')),
                             'strong_auth_required':bool(plan.get('requires_strong_auth'))},
            'blockers':recovery.get('blockers',[]),'status':status,'responsible_party':'user' if status in {'blocked','needs_clarification','waiting_confirmation'} else 'platform',
            'receipts':[],'execution_scope':'LOCAL_DEMO','read_only':True}
        self._save_locked(value,'checkpoint')

    def _save_locked(self,value,stage):
        task_id=value['task_id'];row=self.audit.db.execute('SELECT actor_id,device_id,payload_json,version,created_at FROM business_tasks WHERE task_id=?',(task_id,)).fetchone()
        if row and (row[0],row[1])!=(value['actor_id'],value['device_id']):raise ValueError('Task owner is immutable')
        previous=json.loads(row[2]) if row else {}
        if not value.get('receipts'):value['receipts']=previous.get('receipts',[])
        if previous==value:return
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True)
        if len(raw.encode())>65536:raise ValueError('Task header exceeds bounded projection')
        stamp=_now();version=row[3]+1 if row else 1
        self.audit.db.execute('''INSERT INTO business_tasks VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            payload_json=excluded.payload_json,version=excluded.version,updated_at=excluded.updated_at''',
            (task_id,value['actor_id'],value['device_id'],raw,version,row[4] if row else stamp,stamp))
        self.audit.db.execute('INSERT INTO business_task_events VALUES (?,?,?,?,?)',(task_id,version,value['status'],stage,stamp))

    def response(self,response,stage):
        plan=response.get('plan',{});task_id=plan.get('plan_id')
        if not task_id:return None
        with self.audit.lock,self.audit.db:
            row=self.audit.db.execute('SELECT payload_json FROM business_tasks WHERE task_id=?',(task_id,)).fetchone()
            if not row:return None
            value=json.loads(row[0]);receipts=list(value['receipts'])
            if response.get('error_code') in {'EFFECT_BINDING_MISMATCH','EFFECT_REVIEW_REQUIRED','BUSINESS_DEADLINE_EXPIRED'}:
                value['status']='blocked';value['responsible_party']='user'
                value['blockers']=[{'check':'effect_scope','status':'unknown','message':('最晚提交时间已过，请重新核对。' if response.get('error_code')=='BUSINESS_DEADLINE_EXPIRED' else '动作范围或收款资料需要重新核对。')}]+[x for x in value['blockers'] if x.get('check')!='effect_scope']
            for item in response.get('result',{}).get('results',[]):
                if not isinstance(item,dict):continue
                if item.get('tool') not in {'create_transfer','query_transaction'}:continue
                data=item.get('data') or {}
                if not isinstance(data,dict):continue
                reference={k:data[k] for k in ('transaction_id','request_id','bank_request_id','status') if isinstance(data.get(k),str) and len(data[k])<=128}
                if reference:
                    reference['source']='tool:'+item['tool']
                    if reference not in receipts:receipts.append(reference)
            value['receipts']=receipts[-20:];self._save_locked(value,stage)
        return self.get(task_id,value['actor_id'],value['device_id'])

    def get(self,task_id,actor_id,device_id):
        with self.audit.lock:
            row=self.audit.db.execute('SELECT payload_json,version,created_at,updated_at FROM business_tasks WHERE task_id=? AND actor_id=? AND device_id=?',(task_id,actor_id,device_id)).fetchone()
            if not row:return None
            value=json.loads(row[0]);value.update(version=row[1],created_at=row[2],updated_at=row[3])
            events=self.audit.db.execute('SELECT seq,status,stage,timestamp FROM business_task_events WHERE task_id=? ORDER BY seq DESC LIMIT 100',(task_id,)).fetchall()
            value['timeline']=[dict(zip(('seq','status','stage','timestamp'),e)) for e in reversed(events)]
            return value

    def list(self,actor_id,device_id,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Invalid task page size')
        with self.audit.lock:
            rows=self.audit.db.execute('SELECT task_id FROM business_tasks WHERE actor_id=? AND device_id=? ORDER BY updated_at DESC LIMIT ?',(actor_id,device_id,limit)).fetchall()
            return [self.get(row[0],actor_id,device_id) for row in rows]
