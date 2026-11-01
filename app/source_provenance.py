"""Owned metadata receipts, never raw material or financial authorization."""
import hashlib,json,re
from datetime import datetime,timezone
class SourceProvenance:
 def __init__(self,runtime):
  self.rt=runtime;self.audit=runtime.audit
  with self.audit.lock,self.audit.db:self.audit.db.execute('CREATE TABLE IF NOT EXISTS task_sources (source_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,actor_id TEXT NOT NULL,device_id TEXT NOT NULL,payload_json TEXT NOT NULL,UNIQUE(task_id,actor_id,device_id,source_id))')
 def record(self,kind,task,trace,actor,device,evidence):
  keys={'voice':{'upload_sha256','pcm_sha256','normalized_wav_sha256','transcript_sha256','source_trace_id','draft_changed'},'ocr':{'sha256','reviewed_fields_sha256','field_evidence','source_trace_id','draft_changed'},'rag':{'knowledge_version','citations','scope','family_context'}}
  if kind not in keys or not isinstance(evidence,dict) or set(evidence)-keys[kind] or not task or not actor or not device:raise ValueError('Invalid evidence schema')
  for key in evidence:
   if key.endswith('sha256') and not re.fullmatch('[0-9a-f]{64}',str(evidence[key])):raise ValueError('Invalid source hash')
  raw=json.dumps(evidence,ensure_ascii=False,sort_keys=True,allow_nan=False)
  if len(raw.encode())>16384:raise ValueError('Evidence limit exceeded')
  sid='source_'+hashlib.sha256(json.dumps([task,trace,actor,device,kind,raw]).encode()).hexdigest()[:24].translate(str.maketrans('0123456789abcdef','abcdefghijklmnop'))
  value={'source_id':sid,'kind':kind,'task_id':task,'trace_id':trace,'evidence':evidence,'observed_at':datetime.now(timezone.utc).isoformat(),'authority':'reference_only','raw_material_retained':False}
  with self.audit.lock,self.audit.db:
   self.audit.db.execute('INSERT OR IGNORE INTO task_sources VALUES (?,?,?,?,?)',(sid,task,actor,device,json.dumps(value,ensure_ascii=False)))
   self.audit.write(trace,'source.reference.recorded',{'source_id':sid,'kind':kind,'authority':'reference_only'},actor=actor,intent='material_reference',commit=False)
  return value
 def for_task(self,task,actor,device):
  with self.audit.lock:rows=self.audit.db.execute('SELECT payload_json FROM task_sources WHERE task_id=? AND actor_id=? AND device_id=? LIMIT 20',(task,actor,device)).fetchall()
  return [self.reference(json.loads(row[0])['source_id'],actor,device) for row in rows]
 def get(self,sid,actor,device):
  if not isinstance(sid,str) or not re.fullmatch('source_[a-p]{24}',sid):raise ValueError('Invalid source ID')
  with self.audit.lock:row=self.audit.db.execute('SELECT payload_json FROM task_sources WHERE source_id=? AND actor_id=? AND device_id=?',(sid,actor,device)).fetchone()
  if not row:raise PermissionError('Source unavailable')
  return json.loads(row[0])
 def reference(self,sid,actor,device):
  value=self.get(sid,actor,device)
  try:self.validate(sid,actor,device);return dict(value,current_access=True)
  except (ValueError,PermissionError):return {k:value[k] for k in ('source_id','kind','task_id','observed_at','authority')}|{'current_access':False,'evidence':{},'notice':'资料权限、版本或有效期已变化，请重新核对。'}
 def validate(self,sid,actor,device):
  value=self.get(sid,actor,device)
  if value['kind']=='rag':
   from knowledge_service import KnowledgeScope
   e=value['evidence'];scope=KnowledgeScope(**e['scope']);service=self.rt.knowledge
   if not service._current() or not service._scope_valid(actor,scope):raise ValueError('Source or scope is no longer current')
   family=e.get('family_context')
   if family and self.rt.policy.classify('knowledge_query',{'_family':family},scope.subject_id).get('authz_allowed') is False:raise PermissionError('Family source permission revoked')
   for citation in e['citations']:
    chunk=next((x for x in service._chunks if x.chunk_id==citation['chunk_id'] and x.document_id==citation['document_id']),None)
    if not chunk or chunk.valid_until<datetime.now(timezone.utc).date() or not service._visible(chunk,scope) or chunk.citation()['content_sha256']!=citation['content_sha256'] or chunk.source_sha256!=citation['source_sha256']:raise ValueError('Citation changed or access revoked')
  return value
 def capture_rag(self,plan,result,scope):
  if result.get('status')!='ANSWERED' or not result.get('citations'):return None
  citations=[{k:c[k] for k in ('document_id','chunk_id','source_path','line_start','version','source_sha256','content_sha256') if k in c} for c in result['citations']]
  family=plan.entities.get('_family',{});device=family.get('guardian_device_id',plan.device_id)
  return self.record('rag',plan.plan_id,plan.trace_id,scope.actor_id,device,{'knowledge_version':result['knowledge_version'],'citations':citations,'scope':{'actor_id':scope.actor_id,'subject_id':scope.subject_id,'delegated':scope.delegated},'family_context':family})
