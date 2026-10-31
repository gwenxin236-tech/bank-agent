#!/usr/bin/env python3
"""Bounded READ-ONLY load of the already provisioned local synthetic core.

No raw URL input, HTTP writes, real bank, account creation or credential output.
Reuse P07's fixed FineractAPI; this is not an end-to-end transfer capacity claim.
"""
import argparse,concurrent.futures,hashlib,json,os,platform,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'app'),str(ROOT/'scripts')]
from fineract_transport import FineractAPI,cents
from platform_load_test import summarize,validate_plan,is_timeout

def local_scope(directory,output):
    directory=Path(directory).resolve();output=Path(output).resolve()
    if not directory.is_relative_to(ROOT/'data/verification') or not output.is_relative_to(ROOT/'data/verification'):
        raise ValueError('owned local verification paths required')
    return directory,output

def run(directory,output,plan):
    directory,output=local_scope(directory,output);validate_plan(plan)
    cfg=json.loads((directory/'private.json').read_text(encoding='utf-8'))
    for name in ('source_account_id','target_account_id'):
        if type(cfg[name]) is not int or cfg[name]<=0:raise ValueError('invalid synthetic account mapping')
    # FineractAPI itself refuses non-literal-loopback targets and redirects.
    api=FineractAPI(cfg['base_url'],cfg['username'],cfg['password'])
    def balances():return {r:cents(api('GET','/savingsaccounts/'+str(cfg[r+'_account_id']))['summary']['availableBalance']) for r in ('source','target')}
    report={'status':'RUNNING','scope':'raw native read API on pinned self-hosted synthetic core; no platform transfer/load or licensed-bank claim',
        'frozen_plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(),
        'preregistered_gate':'single-user read p95 from config/performance_plan.json; errors/timeouts zero; both balances unchanged',
        'hardware':{'platform':platform.platform(),'python':sys.version,'logical_cpus':os.cpu_count()},'stages':[]}
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise ValueError('never overwrite a native-core load record')
    def save():output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        before=balances();report['before_cents']=before
        for level in plan['concurrency']:
            start=time.monotonic();deadline=start+plan['stage_seconds']
            def worker(_):
                rows=[]
                # At most 50 requests/worker, 750 total across 1/2/4/8.
                while time.monotonic()<deadline and len(rows)<50:
                    t=time.perf_counter()
                    try:
                        with api.deadline(time.monotonic()+plan['http_timeout_seconds']):body=api('GET','/savingsaccounts/'+str(cfg['source_account_id']))
                        ok=body.get('status',{}).get('active') and cents(body['summary']['availableBalance'])==before['source']
                        row={'ms':(time.perf_counter()-t)*1000,'status':200,'ok':bool(ok)}
                    except Exception as e:row={'ms':(time.perf_counter()-t)*1000,'status':api.last_error.get('status',0) if api.last_error else 0,'ok':False,'timeout':is_timeout(e),'error_type':type(e).__name__}
                    rows.append(row)
                return rows
            with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:rows=[r for group in pool.map(worker,range(level)) for r in group]
            metrics=summarize(rows,time.monotonic()-start);metrics.update(concurrency=level,requested_seconds=plan['stage_seconds'],request_cap_per_worker=50,budget_exhausted=len(rows)>=level*50);report['stages'].append(metrics);save()
        report['after_cents']=balances();single=report['stages'][0]
        report['gates']={'single_read_latency':single['p95_ms']<=plan['gates']['single_user_read_p95_ms'],
            'no_read_errors':all(s['errors']==0 and s['timeouts']==0 for s in report['stages']),
            'no_bank_side_effect':report['before_cents']==report['after_cents']}
        report['status']='PASS_LOCAL_CORE_READ_SCOPE' if all(report['gates'].values()) else 'FAIL_GATES'
    except Exception as e:report.update(status='FAIL_RUN',error_type=type(e).__name__)
    save();return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--plan',type=Path,default=ROOT/'config/performance_plan.json');a=p.parse_args()
    report=run(a.directory,a.output,json.loads(a.plan.read_text(encoding='utf-8')));print(json.dumps({'status':report['status'],'stages':len(report['stages'])}));return 0 if report['status']=='PASS_LOCAL_CORE_READ_SCOPE' else 1

if __name__=='__main__':raise SystemExit(main())
