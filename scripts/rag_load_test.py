#!/usr/bin/env python3
"""Bounded local CPU/Mock load tests. Never targets a bank/API or daily DB."""
import argparse,concurrent.futures,hashlib,json,math,os,platform,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app')]

def percentile(values,percent):
    if not values:return None
    data=sorted(values);at=(len(data)-1)*percent/100;lo=math.floor(at);hi=math.ceil(at)
    return data[lo]+(data[hi]-data[lo])*(at-lo)

def summarize(rows,seconds):
    values=[r['ms'] for r in rows]
    return {'requests':len(rows),'elapsed_seconds':round(seconds,3),'throughput_rps':len(rows)/seconds if seconds>0 else None,
        'p50_ms':percentile(values,50),'p95_ms':percentile(values,95),'p99_ms':percentile(values,99),
        'errors':sum(not r['ok'] for r in rows),'degraded':sum(r['degraded'] for r in rows),
        'rate_limited':sum(r['busy'] for r in rows),'unknown':sum(r.get('unknown',False) for r in rows),
        'timeouts':sum(r.get('timeout',False) for r in rows),'effective_rerank_rate':sum(r.get('reranked',False) for r in rows)/len(rows) if rows else None}

def make_corpus(count):
    # <=100 reviewed synthetic documents, split into bounded chunks for bigger tests.
    if not 1<=count<=1024:raise ValueError('corpus target outside test budget')
    n=min(count,100);docs=[]
    for i in range(n):
        copies=max(1,math.ceil(count/n))
        sentence='This synthetic process note explains that reviewed evidence cannot authorize payment. Confirm the original request and inspect the bank tool status. Keep audit trails and do not repeat an uncertain transaction. '
        block='办理事项与服务流程演示。'+(sentence*2)[:270]+'。\n'
        text=block*copies
        docs.append({'id':f'load_{i}','title':f'合成流程{i}','kind':'process_path','version':'synthetic',
            'reviewed':True,'access':'public','readers':[],'valid_until':'2099-01-01','patterns':['办理事项','服务流程'],'text':text})
    return {'schema':1,'version':'synthetic-load','documents':docs}

def memory_bytes():
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in
                ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        c=Counters();c.cb=ctypes.sizeof(c)
        kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE
        read=ctypes.WinDLL('psapi').GetProcessMemoryInfo
        read.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD];read.restype=wintypes.BOOL
        if read(kernel.GetCurrentProcess(),ctypes.byref(c),c.cb):return c.WorkingSetSize
        return None
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    except ImportError:return None

def run_stage(service,concurrency,seconds):
    start=time.monotonic();cpu=time.process_time();deadline=start+seconds
    queries=['什么是冷静期','点了确认是否代表银行已经办成','一个户头管理不同定期存款','停掉下次扣钱后会员还能用吗']
    def worker(index):
        rows=[]
        while time.monotonic()<deadline:
            before=time.monotonic()
            try:
                a=service.answer(queries[(index+len(rows))%len(queries)],'demo-user');r=a.get('retrieval',{});rr=r.get('rerank',{})
                applied=rr.get('applied',False);rows.append({'ms':(time.monotonic()-before)*1000,
                    'ok':a['status']=='ANSWERED','degraded':not applied,'busy':rr.get('reason')=='busy','reranked':applied})
            except TimeoutError:rows.append({'ms':(time.monotonic()-before)*1000,'ok':False,'degraded':True,'busy':False,'timeout':True})
            except Exception:rows.append({'ms':(time.monotonic()-before)*1000,'ok':False,'degraded':True,'busy':False})
        return rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        batches=list(pool.map(worker,range(concurrency)))
    result=summarize([row for batch in batches for row in batch],time.monotonic()-start)
    result.update(concurrency=concurrency,cpu_seconds=time.process_time()-cpu,process_memory_bytes=memory_bytes())
    return result

def mock_transaction_probe(folder):
    os.environ['BANK_AGENT_DB_PATH']=str(folder/'global.sqlite3')
    from banking_agent import AgentRuntime,runtime
    rt=AgentRuntime(db_path=folder/'bank.sqlite3',offline=True)
    try:
        before=rt.bank.balance().data['available_cents'];plans=[];latencies=[]
        for i in range(10):
            start=time.monotonic();out=rt.chat('给妈妈转0.01元');plan=out['plan'];view=out['plan_view']
            assert view['requires_confirmation']
            def confirm(_):return rt.confirm(plan['plan_id'],view['action_token'])
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:replies=list(p.map(confirm,range(4)))
            latencies.append((time.monotonic()-start)*1000);plans.append(plan)
        after=rt.bank.balance().data['available_cents']
        audit=all(rt.audit.verify_trace(p['trace_id'])['valid'] for p in plans)
        return {'logical_transfers':10,'confirmation_attempts':40,'expected_debit_cents':10,'actual_debit_cents':before-after,
                'duplicate_debits':max(0,before-after-10),'audit_valid':audit,'p95_ms':percentile(latencies,95),'passed':before-after==10 and audit}
    finally:
        for item in (rt,runtime):item.recovery_scheduler.stop();item.bank.close();item.audit.db.close()

def http_probe(service,folder):
    import threading,urllib.request,urllib.error
    os.environ['BANK_AGENT_DB_PATH']=str(folder/'http-global.sqlite3')
    import banking_agent as ba
    rt=ba.AgentRuntime(db_path=folder/'http-bank.sqlite3',offline=True);rt.knowledge=service;rt.knowledge_use_model=False
    previous=(ba.runtime,ba.gateway);ba.runtime=rt;ba.gateway=ba.AgentGateway(rt)
    class Quiet(ba.Handler):
        def log_message(self,*args):pass
    server=ba.ThreadingHTTPServer(('127.0.0.1',0),Quiet);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    before=rt.bank.balance().data['available_cents'];traces=[];opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        def request(_):
            started=time.monotonic();code=0
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/chat',data=json.dumps({'message':'知识问答：什么是冷静期'}).encode(),
                    headers={'Content-Type':'application/json','Authorization':'Bearer demo-user-session'})
                with opener.open(req,timeout=15) as response:body=json.load(response);code=response.status
                data=body['result']['results'][0]['data'];traces.append(body['trace_id']);rerank=data.get('retrieval',{}).get('rerank',{})
                return {'ms':(time.monotonic()-started)*1000,'ok':data['status']=='ANSWERED','degraded':not rerank.get('applied'),
                    'busy':rerank.get('reason')=='busy','reranked':rerank.get('applied',False),'http_status':code}
            except urllib.error.HTTPError as e:
                code=e.code;e.close()
                return {'ms':(time.monotonic()-started)*1000,'ok':code==429,'degraded':True,'busy':code==429,'http_status':code}
            except Exception:return {'ms':(time.monotonic()-started)*1000,'ok':False,'degraded':True,'busy':False,'http_status':code}
        started=time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(request,range(24)))
        result=summarize(rows,time.monotonic()-started)
        result.update(http_status_counts={str(s):sum(r['http_status']==s for r in rows) for s in {r['http_status'] for r in rows}},
            balance_unchanged=rt.bank.balance().data['available_cents']==before,audit_valid=all(rt.audit.verify_trace(t)['valid'] for t in traces))
        return result
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close();ba.runtime,ba.gateway=previous

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stage-seconds',type=int,default=20);p.add_argument('--soak-seconds',type=int,default=600)
    p.add_argument('--concurrency',nargs='+',type=int,default=[1,2,4,8]);a=p.parse_args()
    output=a.output.resolve();output.relative_to(ROOT/'data/verification')
    if not 1<=a.stage_seconds<=60 or not 1<=a.soak_seconds<=1800 or any(c not in range(1,17) for c in a.concurrency):raise ValueError('load outside local budget')
    from knowledge_service import KnowledgeService
    from local_embeddings import LocalEmbeddingClient
    from local_reranker import LocalReranker
    import socket
    # Every inference stays local. This process may not make even accidental connections.
    original_connect=socket.socket.connect
    def connect(sock,address):
        if not isinstance(address,tuple) or address[0] not in {'127.0.0.1','::1'}:raise OSError('external network forbidden')
        return original_connect(sock,address)
    socket.socket.connect=connect
    policy=json.loads((ROOT/'config/policy.json').read_text(encoding='utf-8'))
    service=KnowledgeService.from_path(ROOT/'config/knowledge.json',policy=policy,embedding_client=LocalEmbeddingClient(ROOT/'models/rag'))
    service.reranker=LocalReranker(ROOT/'models/reranker')
    report={'scope':'isolated local CPU RAG/Mock; no real bank/API/IM/IAM or physical second host claim',
        'hardware':{'platform':platform.platform(),'cpu':platform.processor(),'logical_cpus':os.cpu_count(),'python':sys.version},
        'plan':{'concurrency':a.concurrency,'stage_seconds':a.stage_seconds,'soak_seconds':a.soak_seconds,
                'gates':{'single_user_p95_ms':10000,'single_user_rerank_rate_min':0.95,'errors_max':0,'duplicate_debits_max':0},
                'saturation_rule':'report all fallback and busy responses; do not count them as full reranker capacity'},'stages':[]}
    output.parent.mkdir(parents=True,exist_ok=True)
    def save():output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save() # Freeze goals before model execution.
    start=time.monotonic();answer=service.answer('什么是冷静期','demo-user')
    report['cold_start_ms']=(time.monotonic()-start)*1000;report['cold_rerank']=answer.get('retrieval',{}).get('rerank',{})
    with tempfile.TemporaryDirectory(prefix='rag-scale-') as temporary:
        tmp=Path(temporary);report['corpus_scaling']=[]
        for target in (64,256,1024):
            path=tmp/f'corpus-{target}.json';data=make_corpus(target);path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            test=KnowledgeService.from_path(path,embedding_client=service.embedding_client);test.reranker=None
            start=time.monotonic();a1=test.answer('办理事项需要什么流程','demo-user')
            first=(time.monotonic()-start)*1000;test._vectors.clear();start=time.monotonic();a2=test.answer('办理事项需要什么流程','demo-user')
            report['corpus_scaling'].append({'target':target,'actual_chunks':len(test._chunks),'source_bytes':path.stat().st_size,'available':test.available,
                'cold_index_ms':first,'rebuild_ms':(time.monotonic()-start)*1000,'mode':a2.get('retrieval',{}).get('mode'),'memory_bytes':memory_bytes()})
            save()
        for level in a.concurrency:
            print('LOAD concurrency',level,flush=True);report['stages'].append(run_stage(service,level,a.stage_seconds));save()
        print('SOAK seconds',a.soak_seconds,flush=True);report['soak']=run_stage(service,1,a.soak_seconds);save()
        report['mock_transactions']=mock_transaction_probe(tmp)
        report['http_main_chain']=http_probe(service,tmp)
    single=next((s for s in report['stages'] if s['concurrency']==1),report['soak'])
    report['gates_passed']=(single['p95_ms']<=10000 and single['effective_rerank_rate']>=0.95 and
        all(s['errors']==0 for s in [*report['stages'],report['soak']]) and report['mock_transactions']['passed'] and
        report['cold_rerank'].get('applied') is True and all(s['mode']=='local_vector' for s in report['corpus_scaling']))
    report['gates_passed']=report['gates_passed'] and report['http_main_chain']['balance_unchanged'] and report['http_main_chain']['audit_valid'] and report['http_main_chain']['errors']==0
    report['unmeasured']=['real model API','real bank sandbox','real IM/IAM','physical second computer']
    save();print('Local gates:',report['gates_passed'])

if __name__=='__main__':main()
