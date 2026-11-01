"""Local service records over trusted tools and existing handoff queue. No executor."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,re
from business_tasks import _text
from handoff_contract import HandoffCard
from action_effects import normalize_constraints

class ServiceTasks:
 REASONS={'not_authorized','unfamiliar_merchant','duplicate_charge','refund_pending'}
 def __init__(self,runtime):
  self.rt=runtime;self.audit=runtime.audit
  with self.audit.lock,self.audit.db:
   self.audit.db.execute('CREATE TABLE IF NOT EXISTS service_tasks (task_id TEXT PRIMARY KEY,source_key TEXT UNIQUE NOT NULL,actor_id TEXT NOT NULL,device_id TEXT NOT NULL,payload_json TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL)')
 def _access(self,actor,device):
  identity=self.rt.identity_service.current_identity() if self.rt.identity_service else None
  if actor!='demo-user' or not isinstance(device,str) or not device or self.rt.bank_service is not None:raise PermissionError('Local service source unavailable for this account/backend')
  if self.rt.identity_service and (not identity or identity['role']!='user' or identity['user_id']!=actor or identity['device_id']!=device):raise PermissionError('Session mismatch')
 def _key(self,actor,device,kind,request):
  if not isinstance(request,str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',request):raise ValueError('Invalid request ID')
  return hashlib.sha256(json.dumps([actor,device,kind,request]).encode()).hexdigest()
 def _load(self,task,actor,device):
  with self.audit.lock:row=self.audit.db.execute('SELECT * FROM service_tasks WHERE task_id=? AND actor_id=? AND device_id=?',(task,actor,device)).fetchone()
  if not row:return None
  value=json.loads(row['payload_json']);value.update(version=row['version'],updated_at=row['updated_at']);return value
 def _save(self,value,event,expected=None):
  value=deepcopy(value);value.pop('version',None);value.pop('updated_at',None);stamp=datetime.now(timezone.utc).isoformat()
  with self.audit.lock,self.audit.db:
   row=self.audit.db.execute('SELECT version,payload_json FROM service_tasks WHERE task_id=?',(value['task_id'],)).fetchone()
   if expected is not None and (type(expected) is not int or not row or row['version']!=expected):raise ValueError('Task version changed')
   version=row['version']+1 if row else 1
   value['timeline']=(value.get('timeline',[])+[{'event':event,'observed_at':stamp}])[-100:]
   raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
   if len(raw.encode())>32768:raise ValueError('Task record budget exceeded')
   self.audit.db.execute('INSERT INTO service_tasks VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json,version=excluded.version,updated_at=excluded.updated_at',(value['task_id'],value['source_key'],value['actor_id'],value['device_id'],raw,version,stamp))
   self.audit.write(value['trace_id'],'service.'+event,{'task_id':value['task_id'],'kind':value['kind'],'business_executed':False},actor=value['actor_id'],intent='service_followup',commit=False)
 def _open(self,key,actor,device,kind,goal):
  tid='service_'+key[:24].translate(str.maketrans('0123456789abcdef','abcdefghijklmnop'))
  old=self._load(tid,actor,device)
  if old:
   if old['goal']!=goal:raise ValueError('Request ID already bound to another goal')
   return old,True
  return {'task_id':tid,'source_key':key,'actor_id':actor,'device_id':device,'kind':kind,'goal':goal,'trace_id':'strace_'+key.translate(str.maketrans('0123456789abcdef','abcdefghijklmnop')),
    'mode':'LOCAL_DEMO_ONLY','business_executed':False,'official_case_id':None,'local_case_id':None,'timeline':[],'business_outcome':'BANK_REVIEW_REQUIRED' if kind=='dispute' else 'PLAN_ONLY'},False
 def _fact(self,transaction,actor):
  result=self.rt.bank.service_transaction_fact(transaction,actor)
  if not result.ok:raise PermissionError('Transaction unavailable to this account')
  item=result.data
  if type(item.get('amount_cents')) is not int:raise ValueError('Unverified transaction amount')
  return {'transaction_id':transaction,'amount_cents':item['amount_cents'],'merchant':_text(item.get('merchant'),80),'date':_text(item.get('date'),32),'status':_text(item.get('status','posted_record'),32),
          'source':'MockBankCore.service_transaction_fact','observed_at':datetime.now(timezone.utc).isoformat()}
 def dispute(self,transaction,reason,request,actor,device):
  self._access(actor,device)
  if not isinstance(transaction,str) or not 1<=len(transaction)<=128 or reason not in self.REASONS:raise ValueError('Invalid transaction or claim')
  key=self._key(actor,device,'dispute',request)
  with self.rt.recovery_task_lock('service:'+key):
   value,old=self._open(key,actor,device,'dispute',{'transaction_id':transaction,'reason':reason})
   if not old:
    value.update(bank_fact=self._fact(transaction,actor),user_claim=reason,refund_status='UNVERIFIED',protection_status='SEPARATE_OWNER_CONFIRMATION_REQUIRED')
    self._save(value,'dispute_recorded')
   return self.get(value['task_id'],actor,device)
 def funding(self,target,reserve,deadline,request,actor,device):
  self._access(actor,device)
  if type(target) is not int or not 0<target<=2**63-1:raise ValueError('Positive integer target required')
  constraints=normalize_constraints({'reserve_cents':reserve,'deadline':deadline});constraints['no_borrow']=True
  key=self._key(actor,device,'funding',request)
  with self.rt.recovery_task_lock('service:'+key):
   value,old=self._open(key,actor,device,'funding',{'target_cents':target,**constraints})
   if not old:
    report=self.rt.bank.financial_health_report(actor,target_cents=target,reserve_cents=reserve,deadline=constraints['deadline'])
    if not report.ok:raise ValueError('Funding source unavailable')
    value.update(constraints=constraints,funding_plan=report.data['funding_plan']);self._save(value,'funding_prepared')
   return self.get(value['task_id'],actor,device)
 def handoff(self,task,actor,device):
  self._access(actor,device)
  with self.rt.recovery_task_lock('service:'+task):
   value=self._load(task,actor,device)
   if not value:raise PermissionError('Task unavailable')
   if not value.get('local_case_id'):
    card=HandoffCard(user_goal='核对用户提出的交易异议' if value['kind']=='dispute' else '核对期限与资金缺口',verified_information=value.get('bank_fact',value.get('funding_plan',{})),authorization_scope={'scope':'只提交本地人工核查事项，不授权账户或资金操作'},business_identifiers={'service_task_id':task,'trace_id':value['trace_id'],'source_ids':value.get('source_ids',[])},pending_items=['用户主张：'+value.get('user_claim','资金准备请求'),'核实实际银行允许路径；退款、保护与案件受理分别留证'],redacted_context={'trigger':'service_followup','source':'local_demo'})
    case=self.rt.handoffs.enqueue(card.to_dict(),actor,device,value['trace_id'],'service_followup');value['local_case_id']=case['case_id'];self._save(value,'handoff_requested',value['version'])
   return self.get(task,actor,device)
 def payment_handoff(self,task,actor,device):
  self._access(actor,device)
  with self.rt.recovery_task_lock(task):
   entry=self.rt.recovery_store.get(task);raw=entry[1]['plan'] if entry else {}
   if not raw or raw['actor_id']!=actor or raw['device_id']!=device or raw['intent']!='transfer':raise PermissionError('Payment task unavailable')
   card=self.rt._create_handoff_card('unknown_result' if entry[0] in {'processing','unknown','recovery_required'} else 'payment_blocked',plan=raw,actor_id=actor,device_id=device,trace_id=raw['trace_id'],pending_items=['核对当前限额、余额、预算与账户限制','未接入线上调额申请；人工接收不代表付款完成'])
   return {'ok':True,'handoff_card':card,'local_case':self.rt.handoffs.status(card['local_case_id'],actor,device),'business_executed':False}
 def recheck(self,task,version,actor,device):
  self._access(actor,device)
  with self.rt.recovery_task_lock('service:'+task):
   value=self._load(task,actor,device)
   if not value:raise PermissionError('Task unavailable')
   if type(version) is not int or value['version']!=version:raise ValueError('Task version changed')
   if value['kind']=='funding':
    goal=value['goal'];result=self.rt.bank.financial_health_report(actor,target_cents=goal['target_cents'],reserve_cents=goal['reserve_cents'],deadline=goal['deadline'])
    if not result.ok:raise ValueError('Funding source unavailable')
    value['funding_plan']=result.data['funding_plan']
   else:value['bank_fact']=self._fact(value['goal']['transaction_id'],actor)
   self._save(value,'facts_rechecked',version);return self.get(task,actor,device)
 def attach_source(self,task,source,version,actor,device):
  self._access(actor,device)
  with self.rt.recovery_task_lock('service:'+task):
   value=self._load(task,actor,device)
   if not value:raise PermissionError('Task unavailable')
   if type(version) is not int or value['version']!=version:raise ValueError('Task version changed')
   self.rt.sources.validate(source,actor,device)
   ids=value.get('source_ids',[])
   if source not in ids:
    if len(ids)>=10:raise ValueError('Source limit exceeded')
    value['source_ids']=ids+[source];self._save(value,'material_reference_added',version)
   return self.get(task,actor,device)
 def get(self,task,actor,device):
  self._access(actor,device);value=self._load(task,actor,device)
  if not value:return None
  value.pop('source_key',None)
  value['sources']=[self.rt.sources.reference(sid,actor,device) for sid in value.get('source_ids',[])] if hasattr(self.rt,'sources') else []
  if value.get('local_case_id'):value['local_case']=self.rt.handoffs.status(value['local_case_id'],actor,device)
  value['notice']='本地服务记录；人工接收或结单不代表银行争议受理、问题解决或资金退回。'
  return value
 def list(self,actor,device):
  self._access(actor,device)
  with self.audit.lock:rows=self.audit.db.execute('SELECT task_id FROM service_tasks WHERE actor_id=? AND device_id=? ORDER BY updated_at DESC LIMIT 50',(actor,device)).fetchall()
  return [self.get(row[0],actor,device) for row in rows]
