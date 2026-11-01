#!/usr/bin/env python3
"""Bounded owned HTTP prototype load, reusing the original P10 harness."""
import argparse,hashlib,json,os,socket,sys,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'app'),str(ROOT/'scripts')]
from ci_verify import safe_environment
from platform_load_test import Harness,run_stage,summarize,guard_connect

def validate_profile(p):
 keys={'schema','scope','concurrency','stage_seconds','stage_operation_cap','soak_seconds','soak_operation_cap','target_rps','http_timeout_seconds','p95_ms_max','p99_ms_max','rss_peak_bytes_max','rss_growth_bytes_max','unmeasured'}
 if set(p)!=keys or p['schema']!=1 or not p['concurrency'] or len(p['concurrency'])>4 or any(type(c) is not int or not 1<=c<=8 for c in p['concurrency']):raise ValueError('Unsafe profile')
 for key,lo,hi in [('stage_seconds',1,60),('stage_operation_cap',20,200),('soak_seconds',1,300),('soak_operation_cap',20,1000),('http_timeout_seconds',1,30)]:
  if type(p[key]) is not int or not lo<=p[key]<=hi:raise ValueError('Unsafe bounded budget')
 if type(p['target_rps']) not in (int,float) or not 0<p['target_rps']<=5:raise ValueError('Unsafe target rate')
 return p
class PrototypeHarness(Harness):
 def __init__(self,folder,plan):
  super().__init__(folder,dict(plan,capacity_http_limit=100000));self.ordinal=0;self.workflows=set();self.ordinal_lock=threading.Lock();self.transaction=self.rt.bank.recent_transactions().data['items'][0]['id']
 def operation(self,index):
  with self.ordinal_lock:index=self.ordinal;self.ordinal+=1
  workflow=index%5;kind='transfer' if workflow==3 else 'read' if workflow==0 else 'support';start=time.perf_counter();last={'status':0};ok=False;debit=0;unknown=False
  try:
   if workflow==0:last,data=self.request('/api/v1/service-tasks');ok=last['ok'] and isinstance(data.get('items'),list)
   elif workflow in {1,4}:
    last,data=self.request('/api/v1/service-tasks/dispute',{'transaction_id':self.transaction,'reason':'not_authorized','request_id':'load-dispute-'+str(index)});ok=last['ok'] and data['service_task']['official_case_id'] is None
    if workflow==4 and ok:last,data=self.request('/api/v1/service-tasks/handoff',{'task_id':data['service_task']['task_id']});ok=last['ok'] and data['service_task']['local_case']['status']=='LOCAL_QUEUED'
   elif workflow==2:
    last,data=self.request('/api/v1/service-tasks/funding',{'target_cents':100,'reserve_cents':100000,'deadline':'2030-11-01T00:00:00+08:00','request_id':'load-funding-'+str(index)});ok=last['ok'] and data['service_task']['constraints']['no_borrow'] and not data['business_executed']
   else:
    last,data=self.request('/api/chat',{'message':'给妈妈转0.01元'});pid=data['plan']['plan_id'];token=data['plan_view']['action_token'];last,proposal=self.request('/api/dag/propose',{'plan_id':pid,'mode':'cross_domain'});last,adopted=self.request('/api/dag/adopt',{'plan_id':pid,'action_token':token,'proposal':proposal['proposal']});body={'plan_id':pid,'action_token':adopted['plan_view']['action_token']}
    last,done=self.request('/api/confirm',body);state=done.get('result',{}).get('status');unknown=state in {'UNKNOWN','PROCESSING'};ok=last['ok'] and state=='SUCCESS';debit=1 if ok else 0
    if ok:last,replay=self.request('/api/confirm',body);last['expected_rejection']=not replay.get('ok',True);ok=not replay.get('ok',True)
  except (KeyError,ValueError,TypeError):ok=False
  if ok:
   with self.ordinal_lock:self.workflows.add(workflow)
  return {'operation':kind,'workflow':workflow,'ok':bool(ok),'status':last['status'],'ms':(time.perf_counter()-start)*1000,'timeout':last.get('timeout',False),'unknown':unknown,'expected_debit_cents':debit}

def run_profile(plan,folder):
 validate_profile(plan);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);owned='banking_agent' not in sys.modules
 env=safe_environment(os.environ,folder/'import.sqlite3');env.update(BANK_SERVICE_MODE='mock',RAG_EMBEDDING_BACKEND='none',RAG_USE_RERANKER='0')
 with patch.dict(os.environ,env),patch.object(socket.socket,'connect',guard_connect):
  import banking_agent as ba
  h=PrototypeHarness(folder,plan)
  try:
   stages=[run_stage(h,level,plan['stage_seconds'],plan['stage_operation_cap']) for level in plan['concurrency']]
   soak=run_stage(h,4,plan['soak_seconds'],plan['soak_operation_cap'],plan['target_rps']);all_stages=stages+[soak];expected=sum(s['expected_debit_cents'] for s in all_stages);correct=h.correctness(expected);http=summarize(h.http_rows,sum(s['elapsed_seconds'] for s in all_stages));rss=[r['rss_bytes'] for s in all_stages for r in s['resources'] if r['rss_bytes'] is not None];soak_rss=[r['rss_bytes'] for r in soak['resources'] if r['rss_bytes'] is not None]
   gates={'all_workflows_covered':h.workflows==set(range(5)),'all_stages_complete':len(stages)==len(plan['concurrency']) and bool(soak['requests']),'no_logical_errors':all(s['errors']==0 and s['unknown']==0 and s['timeouts']==0 for s in all_stages),'single_debit_and_audit':correct['exact_ledger_match'] and correct['duplicate_debit_cents']==0 and correct['audit_whole_prefix_valid'],'latency':all(s['p95_ms'] is not None and s['p95_ms']<=plan['p95_ms_max'] and s['p99_ms']<=plan['p99_ms_max'] for s in all_stages),'resources':bool(rss and soak_rss) and max(rss)<=plan['rss_peak_bytes_max'] and max(0,soak_rss[-1]-soak_rss[0])<=plan['rss_growth_bytes_max']}
   return {'status':'PASS_LOCAL_SCOPE' if all(gates.values()) else 'FAIL_GATES','stages':stages,'soak':soak,'correctness':correct,'http':http,'gates':gates,'rss_peak_bytes':max(rss) if rss else None,'cloud_calls':0,'capacity_limiter_override':100000,'unmeasured':plan['unmeasured'],'scope':plan['scope']}
  finally:
   h.close()
   if owned:ba.runtime.recovery_scheduler.stop();ba.runtime.bank.close();ba.runtime.audit.db.close()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,default=ROOT/'config/prototype_performance_plan.json');p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve()
 if not out.is_relative_to(ROOT/'data/verification') or out.exists():raise ValueError('Fresh owned output required')
 plan=validate_profile(json.loads(a.plan.read_text(encoding='utf-8-sig')));out.parent.mkdir(parents=True,exist_ok=True);frozen=out.parent/'frozen-prototype-plan.json'
 if frozen.exists():raise ValueError('Frozen plan already exists')
 frozen.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8');daily=ROOT/'data/banking_agent.sqlite3';before=hashlib.sha256(daily.read_bytes()).hexdigest()
 with tempfile.TemporaryDirectory(prefix='prototype-load-') as folder:result=run_profile(plan,folder)
 result.update(frozen_plan_sha256=hashlib.sha256(frozen.read_bytes()).hexdigest(),original_database_unchanged=hashlib.sha256(daily.read_bytes()).hexdigest()==before)
 if not result['original_database_unchanged']:result['status']='FAIL_PROTECTED_DATABASE'
 out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':result['status'],'gates':result['gates']}));return 0 if result['status']=='PASS_LOCAL_SCOPE' else 1
if __name__=='__main__':raise SystemExit(main())
