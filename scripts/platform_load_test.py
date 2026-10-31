#!/usr/bin/env python3
"""P10 bounded closed-loop load on owned loopback services and temporary DBs.

No arbitrary target URL, production data, cloud API or automatic business retry.
Capacity-limiter override is harness-only; default limiter is separately measured.
"""
import argparse,concurrent.futures,contextlib,gc,hashlib,http.cookiejar,io,json,os,platform,socket,subprocess,sys,tempfile,threading,time,uuid,wave
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,build_opener,ProxyHandler,HTTPCookieProcessor
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'app'),str(ROOT/'scripts')]
from rag_load_test import percentile,memory_bytes
_ORIGINAL_CONNECT=socket.socket.connect

def guard_connect(sock,address):
    if not isinstance(address,tuple) or address[0] not in {'127.0.0.1','::1'}:raise OSError('P10 permits owned literal loopback only')
    return _ORIGINAL_CONNECT(sock,address)

def is_timeout(error):
    seen=set()
    while isinstance(error,BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,(TimeoutError,socket.timeout)):return True
        reason=getattr(error,'reason',None)
        error=reason if isinstance(reason,BaseException) else error.__cause__ or error.__context__
    return False

def validate_plan(p):
    allowed={'schema','name','scope','concurrency','stage_seconds','stage_operation_cap','soak_seconds','soak_concurrency','soak_target_rps','soak_operation_cap','http_timeout_seconds','capacity_http_limit','workload_mix','default_limiter_probe_requests','default_limiter_concurrency','auth_accounts','model_queries_per_level','cold_restart_rto_seconds','gates','unmeasured'}
    if set(p)!=allowed or p['schema']!=1:raise ValueError('unsupported or incomplete plan')
    for key,lo,hi in [('stage_seconds',1,60),('stage_operation_cap',10,1000),('soak_seconds',1,600),('soak_operation_cap',10,2000),('soak_concurrency',1,8),('http_timeout_seconds',1,30),('auth_accounts',1,16),('model_queries_per_level',1,16),('default_limiter_probe_requests',121,200)]:
        if type(p[key]) is not int or not lo<=p[key]<=hi:raise ValueError('unsafe test budget: '+key)
    if not p['concurrency'] or any(type(c) is not int or not 1<=c<=8 for c in p['concurrency']) or len(set(p['concurrency']))!=len(p['concurrency']):raise ValueError('unsafe concurrency')
    if p['workload_mix']!={'read':6,'transfer':3,'support':1} or not 0<p['soak_target_rps']<=5 or p['capacity_http_limit']!=100000:raise ValueError('unsupported workload')
    return p

def operation_kind(i):return 'read' if i%10<6 else 'transfer' if i%10<9 else 'support'

def summarize(rows,seconds):
    count=len(rows);successful=sum(bool(r['ok']) for r in rows);limited=sum(r.get('status')==429 for r in rows)
    rejected=sum(bool(r.get('expected_rejection')) and r.get('status')!=429 for r in rows)
    return {'requests':count,'successful':successful,'expected_rejections':rejected,'rate_limited':limited,
        'errors':sum(not r['ok'] and not r.get('expected_rejection') and r.get('status')!=429 for r in rows),
        'timeouts':sum(bool(r.get('timeout')) for r in rows),'unknown':sum(bool(r.get('unknown')) for r in rows),
        'elapsed_seconds':round(seconds,4),'throughput_rps':count/seconds if seconds>0 else None,'goodput_rps':successful/seconds if seconds>0 else None,
        'p50_ms':percentile([r['ms'] for r in rows],50),'p95_ms':percentile([r['ms'] for r in rows],95),'p99_ms':percentile([r['ms'] for r in rows],99),
        'http_status_counts':{str(k):sum(r.get('status')==k for r in rows) for k in sorted({r.get('status',0) for r in rows})}}

def rss():
    try:return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,AttributeError):return memory_bytes()

class Harness:
    def __init__(self,folder,plan,identity=False,limiter=None):
        import banking_agent as ba
        self.ba=ba;self.plan=plan;self.folder=folder;self.operator=uuid.uuid4().hex+uuid.uuid4().hex
        with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted' if identity else 'demo','BANK_AGENT_DEMO_ADMIN_TOKEN':self.operator}):
            self.rt=ba.AgentRuntime(db_path=folder/'runtime.sqlite3',offline=True)
        from security_controls import SlidingWindowRateLimiter
        self.previous=ba.runtime,ba.gateway,ba.http_rate_limiter
        ba.runtime=self.rt;ba.gateway=ba.AgentGateway(self.rt)
        ba.http_rate_limiter=limiter or SlidingWindowRateLimiter(limit=plan['capacity_http_limit'],window_seconds=60)
        class Quiet(ba.Handler):
            def log_message(self,*args):pass
        self.server=ba.ThreadingHTTPServer(('127.0.0.1',0),Quiet);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.base='http://127.0.0.1:'+str(self.server.server_port);self.thread.start();self.traces=[];self.http_rows=[];self.rows_lock=threading.Lock()
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
        self.ba.runtime,self.ba.gateway,self.ba.http_rate_limiter=self.previous
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
    def request(self,path,body=None,*,operator=False,client=None,raw=None,mime='application/json',extra=None):
        start=time.perf_counter();headers={'Authorization':'Bearer '+(self.operator if operator else 'demo-user-session'),'Content-Type':mime,'Origin':self.base,**(extra or {})}
        payload=raw if raw is not None else json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        try:
            request=Request(self.base+path,data=payload,headers=headers)
            with (client or build_opener(ProxyHandler({}))).open(request,timeout=self.plan['http_timeout_seconds']) as response:code=response.status;data=json.load(response)
            row={'status':code,'ok':code==200 and data.get('ok',True),'ms':(time.perf_counter()-start)*1000,'endpoint':path.split('?',1)[0]}
        except HTTPError as e:
            code=e.code
            try:data=json.load(e)
            except Exception:data={}
            e.close();row={'status':code,'ok':False,'ms':(time.perf_counter()-start)*1000,'endpoint':path.split('?',1)[0]}
        except (TimeoutError,socket.timeout):data={};row={'status':0,'ok':False,'timeout':True,'ms':(time.perf_counter()-start)*1000,'endpoint':path.split('?',1)[0]}
        except Exception as e:data={};row={'status':0,'ok':False,'error_type':type(e).__name__,'timeout':is_timeout(e),'ms':(time.perf_counter()-start)*1000,'endpoint':path.split('?',1)[0]}
        with self.rows_lock:self.http_rows.append(row)
        if data.get('trace_id'):self.traces.append(data['trace_id'])
        return row,data
    def operation(self,index):
        kind=operation_kind(index);start=time.perf_counter();timeout=False;unknown=False;ok=False;last={'status':0};debit=0
        try:
            if kind=='read':
                last,out=self.request('/api/chat',{'message':'查询余额'});ok=last['ok'] and out.get('result',{}).get('status')=='SUCCESS'
            elif kind=='transfer':
                last,out=self.request('/api/chat',{'message':'给妈妈转0.01元'});body={'plan_id':out['plan']['plan_id'],'action_token':out['plan_view']['action_token']}
                if not out['plan_view']['requires_confirmation']:raise ValueError('missing consent gate')
                last,done=self.request('/api/confirm',body);state=done.get('result',{}).get('status');unknown=state in {'PROCESSING','UNKNOWN'}
                ok=last['ok'] and state=='SUCCESS';debit=1 if ok else 0
                if ok:
                    replay,again=self.request('/api/confirm',body);replay['expected_rejection']=not again.get('ok',True);ok=not again.get('ok',True)
            else:
                last,out=self.request('/api/v1/customer-service',{'request_id':'p10-'+uuid.uuid4().hex});case=out['handoff_case']['case_id']
                last,out=self.request('/api/v1/handoff/claim',{'case_id':case},operator=True)
                if not last['ok']:raise ValueError('claim failed')
                last,out=self.request('/api/v1/support/send',{'case_id':case,'message_id':'m-'+str(index),'text':'压测虚构留言，不办理业务'})
                if not last['ok']:raise ValueError('send failed')
                last,out=self.request('/api/v1/handoff/resolve',{'case_id':case,'resolution':'GUIDANCE_PROVIDED'},operator=True);ok=last['ok'] and out['case']['status']=='LOCAL_CLOSED'
        except (KeyError,TypeError,ValueError):ok=False
        return {'operation':kind,'ok':ok,'status':last['status'],'ms':(time.perf_counter()-start)*1000,'timeout':last.get('timeout',False),
                'unknown':unknown,'expected_debit_cents':debit}
    def correctness(self,expected):
        balance=self.rt.bank.balance_cents;actual=self.rt.bank.daily_transfer_cents
        row=self.rt.audit.db.execute('SELECT trace_id FROM audit_events WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1').fetchone()
        verified=self.rt.audit.verify_trace(row[0]) if row else {'valid':False,'count':0}
        count=self.rt.bank.db.execute("SELECT count(*) FROM core_transactions WHERE status='succeeded' AND debit_applied=1").fetchone()[0]
        return {'expected_debit_cents':expected,'actual_debit_cents':actual,'core_transaction_count':count,'duplicate_debit_cents':max(0,actual-expected),
                'exact_ledger_match':actual==expected and count==expected,'audit_whole_prefix_valid':verified['valid'] and verified['count']>0,
                'audit_verified_through_last_trace':row[0] if row else None,'available_cents':balance}

def run_stage(h,concurrency,seconds,cap,target_rps=None):
    start=time.monotonic();cpu=time.process_time();counter=0;lock=threading.Lock();samples=[];stop=threading.Event();deadline=start+seconds
    def sample():
        while not stop.wait(1):samples.append({'elapsed_seconds':round(time.monotonic()-start,3),'rss_bytes':rss()})
    sampler=threading.Thread(target=sample,daemon=True);sampler.start();samples.append({'elapsed_seconds':0,'rss_bytes':rss()})
    def worker(_):
        nonlocal counter
        rows=[]
        while time.monotonic()<deadline:
            with lock:
                if counter>=cap:break
                i=counter;counter+=1
            if target_rps:
                delay=start+i/target_rps-time.monotonic()
                if delay>0:stop.wait(delay)
                if time.monotonic()>=deadline:break
            rows.append(h.operation(i))
            if rows[-1]['unknown'] or rows[-1]['timeout']:break # never retry an uncertain write
        return rows
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:rows=[r for group in pool.map(worker,range(concurrency)) for r in group]
    finally:stop.set();sampler.join(timeout=2)
    elapsed=time.monotonic()-start;samples.append({'elapsed_seconds':round(elapsed,3),'rss_bytes':rss()});metrics=summarize(rows,elapsed)
    metrics.update(concurrency=concurrency,requested_seconds=seconds,operation_cap=cap,budget_exhausted=len(rows)>=cap,
        cpu_seconds=time.process_time()-cpu,cpu_percent_one_core=100*(time.process_time()-cpu)/elapsed,resources=samples,
        expected_debit_cents=sum(r['expected_debit_cents'] for r in rows),by_operation={k:summarize([r for r in rows if r['operation']==k],elapsed) for k in ('read','transfer','support')})
    return metrics

def duplicate_and_fault_probes(folder,plan):
    h=Harness(folder,plan);result={}
    try:
        before=h.rt.bank.balance_cents;success=0;started=time.monotonic()
        for i in range(10):
            _,out=h.request('/api/chat',{'message':'给妈妈转0.01元'});body={'plan_id':out['plan']['plan_id'],'action_token':out['plan_view']['action_token']}
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:replies=list(pool.map(lambda _:h.request('/api/confirm',body)[1],range(8)))
            success+=sum(r.get('result',{}).get('status')=='SUCCESS' for r in replies)
        result['duplicate_confirmations']={'logical_transfers':10,'attempts':80,'successes':success,'actual_debit_cents':before-h.rt.bank.balance_cents,'elapsed_seconds':time.monotonic()-started,'passed':success==10 and before-h.rt.bank.balance_cents==10}
        before=h.rt.bank.balance_cents;h.rt.bank.fault_injection='timeout';_,out=h.request('/api/chat',{'message':'给妈妈转1元'});body={'plan_id':out['plan']['plan_id'],'action_token':out['plan_view']['action_token']};_,done=h.request('/api/confirm',body)
        core=h.rt.bank.db.execute('SELECT transaction_id FROM core_transactions ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        processing=done.get('result',{}).get('status')=='PROCESSING' and h.rt.bank.balance_cents==before
        for _ in range(8):h.request('/api/v1/transactions/status?transaction_id='+core)
        unchanged=h.rt.bank.balance_cents==before
        h.rt.bank.finalize_transaction(core,outcome='succeeded');h.rt.bank.fault_injection=None
        h.request('/api/v1/transactions/status?transaction_id='+core)
        result['unknown_result']={'expected_injected_processing':processing,'queries_did_not_retry_debit':unchanged,'explicit_mock_terminal_update':'succeeded','single_terminal_debit':before-h.rt.bank.balance_cents==100,'passed':processing and unchanged and before-h.rt.bank.balance_cents==100}
        before=h.rt.bank.balance_cents;_,out=h.request('/api/chat',{'message':'给妈妈转2000元'});body={'plan_id':out['plan']['plan_id'],'action_token':out['plan_view']['action_token']};_,challenge=h.request('/api/confirm',body)
        for _ in range(3):h.request('/api/verify',dict(body,credential_id=challenge['auth_challenge']['credential_id'],code='000000'))
        result['breaker']={'locked_after_failures':h.rt.breaker.is_locked(),'no_debit':h.rt.bank.balance_cents==before,'passed':h.rt.breaker.is_locked() and h.rt.bank.balance_cents==before}
    finally:h.close()
    return result

def limiter_probe(folder,plan):
    from security_controls import SlidingWindowRateLimiter
    h=Harness(folder,plan,limiter=SlidingWindowRateLimiter());rows=[];start=time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=plan['default_limiter_concurrency']) as pool:
            rows=list(pool.map(lambda _:h.request('/api/chat',{'message':'查询余额'})[0],range(plan['default_limiter_probe_requests'])))
        result=summarize(rows,time.monotonic()-start);result['configured_limit']=h.ba.http_rate_limiter.limit;result['window_seconds']=h.ba.http_rate_limiter.window_seconds
        result['passed']=result['successful']==result['configured_limit'] and result['rate_limited']==len(rows)-result['configured_limit'] and result['errors']==0
        return result
    finally:h.close()

def identity_probe(folder,plan):
    from identity_service import totp
    h=Harness(folder,plan,identity=True);seeds={};passwords={};s=h.rt.identity_service
    try:
        for i in range(plan['auth_accounts']):
            user='perf-user-'+str(i);passwords[user]=uuid.uuid4().hex;seeds[user]=s.provision(user,passwords[user])
        def login(user):
            client=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
            row,out=h.request('/api/v1/identity/login',{'username':user,'password':passwords[user],'code':totp(seeds[user],s.clock())},client=client)
            if row['ok']:row['ok']=h.request('/api/v1/identity/me',client=client)[1].get('user_id')==user
            return row
        start=time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(login,passwords))
        result=summarize(rows,time.monotonic()-start);result['scope']='distinct synthetic users, actual password/TOTP verification; no bank account granted to these identities';result['passed']=result['successful']==len(rows) and result['errors']==0
        return result
    finally:h.close()

def model_probes(folder,plan,ocr_image):
    h=Harness(folder,plan);rag=[];ocr=[];first_cold_ms=None
    try:
        for level in plan['concurrency']:
            start=time.monotonic()
            def query(_):
                row,out=h.request('/api/chat',{'message':'知识问答：什么是冷静期'})
                try:
                    data=out['result']['results'][0]['data'];row['ok']=row['ok'] and data['status']=='ANSWERED' and bool(data['citations']);row['reranked']=data['retrieval'].get('rerank',{}).get('applied',False);row['mode']=data['retrieval'].get('mode')
                except (KeyError,TypeError,IndexError):row['ok']=False;row['reranked']=False
                return row
            with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:rows=list(pool.map(query,range(plan['model_queries_per_level'])))
            if level==1:first_cold_ms=rows[0]['ms']
            stats=summarize(rows,time.monotonic()-start);stats.update(concurrency=level,rerank_applied=sum(r.get('reranked',False) for r in rows),rerank_rate=sum(r.get('reranked',False) for r in rows)/len(rows),degraded=sum(not r.get('reranked',False) for r in rows),fully_reranked_goodput_rps=sum(r.get('reranked',False) and r['ok'] for r in rows)/(time.monotonic()-start),retrieval_modes=sorted({r.get('mode','unavailable') for r in rows}));rag.append(stats)
        h.rt.knowledge._vectors.clear()
        rebuild_row,rebuild=h.request('/api/chat',{'message':'知识问答：什么是冷静期'})
        try:rebuilt=rebuild_row['ok'] and rebuild['result']['results'][0]['data']['retrieval']['mode']=='local_vector' and bool(h.rt.knowledge._vectors)
        except (KeyError,TypeError,IndexError):rebuilt=False
        raw=ocr_image.read_bytes()
        for _ in range(4):
            row,out=h.request('/api/v1/ocr/extract',raw=raw,mime='image/png',extra={'X-OCR-Consent':'yes'})
            row['ok']=row['ok'] and out.get('fields',{}).get('amount_cents')==12345 and out.get('fields',{}).get('recipient')=='妈妈' and out.get('business_executed') is False;ocr.append(row)
        from speech_adapter import prepare_audio
        b=io.BytesIO()
        with wave.open(b,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0'*32000)
        audio=[]
        for _ in range(100):
            start=time.perf_counter();e,_=prepare_audio(b.getvalue());audio.append({'ms':(time.perf_counter()-start)*1000,'ok':bool(e.get('pcm_sha256')),'status':200})
        return {'rag':rag,'rag_cold_first_request_included':True,'rag_first_process_cold_ms':first_cold_ms,'cold_scope':'new process/model sessions; operating-system caches not flushed',
            'rag_clear_index_rebuild_ms':rebuild_row['ms'],'rag_rebuild_verified':rebuilt,
            'ocr':summarize(ocr,sum(x['ms'] for x in ocr)/1000),'ocr_fixture_sha256':hashlib.sha256(raw).hexdigest(),'audio_hash':summarize(audio,sum(x['ms'] for x in audio)/1000),'scope':'local models, fixed synthetic OCR and WAV; no cloud ASR/model request'}
    finally:h.close()

def evaluate_gates(report,plan):
    g=plan['gates'];stages=report.get('stages',[]);soak=report.get('soak',{});correct=report.get('correctness',{});single=next((s for s in stages if s['concurrency']==1),{})
    result={'complete_required_groups':all(k in report for k in ['stages','soak','correctness','limiter','identity','faults','recovery','models','cold_start']),
        'financial_correctness':correct.get('exact_ledger_match',False) and correct.get('audit_whole_prefix_valid',False) and correct.get('duplicate_debit_cents',1)<=g['duplicate_debit_cents_max'],
        'no_unexpected_errors':bool(stages) and all(s.get('errors',1)<=g['errors_max'] and s.get('unknown',1)<=g['unknown_max'] and s.get('timeouts',1)<=g['timeouts_max'] for s in [*stages,soak]),
        'single_user_latency':bool(single) and all((single.get('by_operation',{}).get(k,{}).get('p95_ms') or float('inf'))<=g['single_user_'+k+'_p95_ms'] for k in ('read','transfer','support')) and (single.get('p99_ms') or float('inf'))<=g['single_user_p99_ms'],
        'soak_latency_and_goodput':(soak.get('p95_ms') or float('inf'))<=g['soak_p95_ms'] and (soak.get('goodput_rps') or 0)>=g['soak_goodput_rps_min'] and soak.get('elapsed_seconds',0)>=plan['soak_seconds']-1,
        'default_limiter':report.get('limiter',{}).get('passed',False),'identity':report.get('identity',{}).get('passed',False) and (report.get('identity',{}).get('p95_ms') or float('inf'))<=g['auth_login_p95_ms'],
        'faults_and_breaker':bool(report.get('faults')) and all(x.get('passed',False) for x in report.get('faults',{}).values()),'crash_recovery':report.get('recovery',{}).get('status')=='PASS'}
    resources=[r['rss_bytes'] for s in [*stages,soak] for r in s.get('resources',[]) if r['rss_bytes'] is not None];ss=[r['rss_bytes'] for r in soak.get('resources',[]) if r['rss_bytes'] is not None]
    result['resource_budget']=bool(resources and ss) and max([*resources,report.get('process_peak_rss_bytes') or 0])<=g['rss_peak_bytes_max'] and max(0,ss[-1]-ss[0])<=g['soak_rss_growth_bytes_max']
    models=report.get('models',{});rs=next((x for x in models.get('rag',[]) if x['concurrency']==1),{})
    result['local_models']=bool(rs) and all(x['errors']==0 and x['timeouts']==0 for x in models.get('rag',[])) and rs['p95_ms']<=g['rag_single_p95_ms'] and rs['rerank_rate']>=g['rag_single_rerank_rate_min'] and models.get('ocr',{}).get('errors',1)==0 and (models.get('ocr',{}).get('p95_ms') or float('inf'))<=g['ocr_single_p95_ms'] and models.get('audio_hash',{}).get('errors',1)==0
    result['cold_start_and_rebuild']=bool(report.get('cold_start')) and all(x<=plan['cold_restart_rto_seconds'] for x in report.get('cold_start',[])) and models.get('rag_rebuild_verified',False)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,default=ROOT/'config/performance_plan.json');parser.add_argument('--output',type=Path,required=True);parser.add_argument('--ocr-image',type=Path,required=True);parser.add_argument('--workdir',type=Path);args=parser.parse_args()
    out=args.output.resolve();image=args.ocr_image.resolve()
    if not out.is_relative_to(ROOT/'data/verification') or not image.is_relative_to(ROOT/'data/verification'):raise ValueError('owned verification paths required')
    workroot=args.workdir.resolve() if args.workdir else None
    if workroot:
        if not workroot.is_relative_to(ROOT/'data') or workroot==ROOT/'data':raise ValueError('owned runtime subdirectory required')
        workroot.mkdir(parents=True,exist_ok=True)
    plan=validate_plan(json.loads(args.plan.read_text(encoding='utf-8')));out.parent.mkdir(parents=True,exist_ok=True)
    frozen=out.parent/'frozen-plan.json'
    if frozen.exists():raise ValueError('never overwrite a frozen performance plan')
    frozen.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8');plan_sha=hashlib.sha256(frozen.read_bytes()).hexdigest()
    original=ROOT/'data/banking_agent.sqlite3';before=hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    report={'status':'RUNNING','scope':plan['scope'],'load_model':'bounded closed-loop; includes client/TCP overhead; not open-arrival SLO proof','frozen_plan_sha256':plan_sha,'plan':plan,'database_work_root':str(workroot) if workroot else 'OS temporary directory (may be RAM-backed)','hardware':{'platform':platform.platform(),'cpu':platform.processor(),'logical_cpus':os.cpu_count(),'python':sys.version,'resource_scope':'benchmark process including server and load generator'},'stages':[],'unmeasured':plan['unmeasured']}
    for key,path in [('cgroup_cpu_max','/sys/fs/cgroup/cpu.max'),('cgroup_memory_max','/sys/fs/cgroup/memory.max')]:
        if Path(path).is_file():report['hardware'][key]=Path(path).read_text().strip()
    if Path('/proc/cpuinfo').is_file():
        report['hardware']['cpu_model']=next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')),'not reported')
    import importlib.metadata
    report['dependency_versions']={}
    for name in ('onnxruntime','tokenizers','numpy','rapidocr','Pillow','opencv-python-headless'):
        try:report['dependency_versions'][name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:report['dependency_versions'][name]='not installed'
    report['source_hashes']={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('app','config') for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.json','.js','.css','.html'}}
    for name in ('scripts/platform_load_test.py','scripts/rag_load_test.py','scripts/drill_recovery.py','models/rag/manifest.json','models/reranker/manifest.json','models/ocr/manifest.json'):
        if (ROOT/name).is_file():report['source_hashes'][name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    def save():out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save();socket.socket.connect=guard_connect
    try:
        with tempfile.TemporaryDirectory(prefix='p10-owned-',dir=workroot) as t:
            folder=Path(t);os.environ.update(BANK_AGENT_DB_PATH=str(folder/'import.sqlite3'),BANK_AGENT_OFFLINE='1',IDENTITY_MODE='demo')
            import banking_agent as ba
            try:
                for label in ['models','capacity','limiter','identity','faults']:(folder/label).mkdir()
                from drill_recovery import RecoveryDrill
                report['cold_start']=[]
                for i in range(3):
                    cold=folder/('cold-'+str(i));cold.mkdir();drill=RecoveryDrill(cold,plan['cold_restart_rto_seconds']);started=time.monotonic()
                    try:drill.start();report['cold_start'].append(time.monotonic()-started)
                    finally:drill.close()
                save()
                print('P10 models',flush=True);report['models']=model_probes(folder/'models',plan,image);save();gc.collect()
                h=Harness(folder/'capacity',plan);expected=0
                try:
                    for level in plan['concurrency']:
                        print('P10 capacity',level,flush=True);stage=run_stage(h,level,plan['stage_seconds'],plan['stage_operation_cap']);report['stages'].append(stage);expected+=stage['expected_debit_cents'];save()
                    print('P10 soak',plan['soak_seconds'],flush=True);report['soak']=run_stage(h,plan['soak_concurrency'],plan['soak_seconds'],plan['soak_operation_cap'],plan['soak_target_rps']);expected+=report['soak']['expected_debit_cents'];report['correctness']=h.correctness(expected);report['http']=summarize(h.http_rows,sum(s['elapsed_seconds'] for s in [*report['stages'],report['soak']]));save()
                finally:h.close()
                print('P10 default limiter',flush=True);report['limiter']=limiter_probe(folder/'limiter',plan);save()
                print('P10 identity',flush=True);report['identity']=identity_probe(folder/'identity',plan);save()
                print('P10 faults',flush=True);report['faults']=duplicate_and_fault_probes(folder/'faults',plan);save()
                print('P10 crash recovery',flush=True)
                recovery_env=dict(os.environ,IDENTITY_MODE='demo')
                if workroot:recovery_env.update(TMPDIR=str(workroot),TEMP=str(workroot),TMP=str(workroot))
                r=subprocess.run([sys.executable,str(ROOT/'scripts/drill_recovery.py'),'--rto-target-seconds',str(plan['cold_restart_rto_seconds'])],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,env=recovery_env)
                report['recovery']=json.loads(r.stdout.decode('utf-8'));report['recovery']['returncode']=r.returncode
                try:
                    import resource
                    report['process_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
                except ImportError:report['process_peak_rss_bytes']=None
                report['gates']=evaluate_gates(report,plan);report['status']='PASS_LOCAL_SCOPE' if all(report['gates'].values()) else 'FAIL_GATES'
            finally:ba.runtime.recovery_scheduler.stop();ba.runtime.bank.close();ba.runtime.audit.db.close()
    except Exception as e:
        import traceback
        frame=traceback.extract_tb(e.__traceback__)[-1]
        report.update(status='FAIL_RUN',error_type=type(e).__name__,error_site={'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name})
    finally:
        socket.socket.connect=_ORIGINAL_CONNECT;after=hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None;report['original_database_unchanged']=before==after
        if not report['original_database_unchanged']:report['status']='FAIL_PROTECTED_DATABASE'
        report['frozen_plan_unchanged']=hashlib.sha256(frozen.read_bytes()).hexdigest()==plan_sha;save();print(json.dumps({'status':report['status'],'gates':report.get('gates'),'error_type':report.get('error_type')}))
    return 0 if report['status']=='PASS_LOCAL_SCOPE' else 1

if __name__=='__main__':raise SystemExit(main())
