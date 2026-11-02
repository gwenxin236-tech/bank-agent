#!/usr/bin/env python3
"""Offline ranking/answerability evaluation and attested human-sample import.

No LLM judge, network transport, user recruiting or identity verification.
"""
import argparse,hashlib,json,math,re,sys,time
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from knowledge_service import KnowledgeService,_private,_unsafe
from local_embeddings import LocalEmbeddingClient
from local_reranker import LocalReranker

TYPES={'answerable','uncovered','out_of_scope','unsafe','private'}

def validate_user_study(data,source_ids):
    if (data.get('schema')!=1 or data.get('provenance')!='participant_submitted' or
        data.get('consent_local_processing') is not True or not isinstance(data.get('samples'),list) or len(data['samples'])>500):
        raise ValueError('invalid provenance/consent/samples')
    rows=[]
    for sample in data['samples']:
        participant=sample.get('participant_id','');q=sample.get('question','');reviewers=sample.get('reviewers',[])
        if (not re.fullmatch(r'p_[A-Za-z0-9]{4,16}',participant) or not isinstance(q,str) or not 0<len(q)<=400 or
            _private(q) or _unsafe(q) or not isinstance(reviewers,list) or len(set(reviewers))<2 or
            any(not isinstance(r,str) or not re.fullmatch(r'p_[A-Za-z0-9]{4,16}',r) for r in reviewers)):
            raise ValueError('private/unreviewed sample')
        date=datetime.fromisoformat(sample.get('submitted_at',''))
        if date.tzinfo is None:raise ValueError('submission timezone required')
        expected=sample.get('relevant_ids');kind=sample.get('expected_type')
        if (kind not in TYPES or not isinstance(expected,list) or any(i not in source_ids for i in expected) or
            bool(expected)!=(kind=='answerable')):raise ValueError('invalid annotation')
        rows.append({'id':'user-'+str(len(rows)+1),'split':'human','type':kind,'question':q,'relevant_ids':expected,
                     'participant_id':participant,'submitted_at':sample['submitted_at'],'reviewers':reviewers})
    return {'status':'READY_ATTESTED' if rows else 'PENDING','identity_verified':False,
            'scope':'participant provenance is supplied by the owner, not independently authenticated','cases':rows}

def metrics(rows):
    positive=[r for r in rows if r['expected']];negative=[r for r in rows if not r['expected']]
    ranks=[next((i for i,d in enumerate(r['hits'][:3],1) if d in r['expected']),None) for r in positive]
    return {'count':len(rows),'hit_at_1':sum(bool(r['hits']) and r['hits'][0] in r['expected'] for r in positive)/len(positive) if positive else None,
        'hit_at_3':sum(r is not None for r in ranks)/len(ranks) if ranks else None,
        'mrr_at_3':sum(1/r if r else 0 for r in ranks)/len(ranks) if ranks else None,
        'ndcg_at_3':sum(1/math.log2(r+1) if r else 0 for r in ranks)/len(ranks) if ranks else None,
        'negative_refusal_rate':sum(r['correct'] for r in negative)/len(negative) if negative else None,
        'correct':sum(r['correct'] for r in rows),'failures':[r.get('id','unlabeled') for r in rows if not r['correct']]}

def evaluation_source(source_path=None):
    source_path=Path(source_path or ROOT/'config/knowledge.json').resolve()
    allowed=[(ROOT/name).resolve() for name in ('config','eval','data/verification')]
    if (source_path.suffix!='.json' or '_internal' in source_path.parts or
        not any(base in source_path.parents for base in allowed)):
        raise ValueError('source outside approved JSON roots')
    if source_path.stat().st_size>524288:raise ValueError('source too large')
    return source_path


def evaluate(dataset,modes=('lexical','hybrid','reranked'),user_study=None,source_path=None):
    source_path=evaluation_source(source_path)
    raw=source_path.read_bytes();source=json.loads(raw);source_ids={d['id'] for d in source['documents']}
    if dataset.get('schema')!=1 or not isinstance(dataset.get('cases'),list):raise ValueError('invalid dataset')
    if dataset.get('source_sha256')!=hashlib.sha256(raw).hexdigest():raise ValueError('dataset/source version mismatch')
    cases=dataset['cases'][:]
    human=validate_user_study(user_study,source_ids) if user_study is not None else {'status':'PENDING','cases':[],'identity_verified':False}
    cases+=human['cases'];seen=set()
    for c in cases:
        if (c['id'] in seen or c['type'] not in TYPES or not isinstance(c['question'],str) or not 0<len(c['question'])<=400 or
            not isinstance(c['relevant_ids'],list) or any(i not in source_ids for i in c['relevant_ids']) or
            bool(c['relevant_ids'])!=(c['type']=='answerable')):raise ValueError('invalid case labels')
        seen.add(c['id'])
    provider=LocalEmbeddingClient(ROOT/'models/rag');ranker=LocalReranker(ROOT/'models/reranker')
    policy=json.loads((ROOT/'config/policy.json').read_text(encoding='utf-8'))
    report={'dataset_version':dataset.get('version'),'dataset_sha256':hashlib.sha256(json.dumps(dataset,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'source_sha256':hashlib.sha256(raw).hexdigest(),'human_status':human['status'],'provenance':dataset.get('provenance'),
        'scope':'synthetic calibration/holdout labels are regression groups, exposed during development; not independent unseen or real-user evidence',
        'independent_holdout':False,'modes':{}}
    for mode in modes:
        service=KnowledgeService.from_path(source_path,policy=policy,embedding_client=None if mode=='lexical' else provider)
        service.reranker=ranker if mode=='reranked' else None
        rows=[];started=time.monotonic();accepted=0
        for case in cases:
            captured=[];original=service.search
            def observe(*args,**kwargs):
                hits=original(*args,**kwargs);captured.extend(hits);return hits
            service.search=observe
            try:answer=service.answer(case['question'],'demo-user')
            finally:service.search=original
            ids=list(dict.fromkeys(c.document_id for c in captured))[:3]
            status=answer['status'];kind=case['type']
            citations=answer.get('citations',[])
            grounded=(isinstance(citations,list) and bool(citations) and all(isinstance(c,dict) and
                any(c.get('document_id')==hit.document_id and c.get('chunk_id')==hit.chunk_id and
                    c.get('quote')==hit.text for hit in captured) for c in citations))
            ok=(status=='ANSWERED' and isinstance(answer.get('answer'),str) and bool(answer['answer'].strip()) and
                bool(ids) and ids[0] in case['relevant_ids'] and grounded and
                any(c['document_id'] in case['relevant_ids'] for c in citations)) if kind=='answerable' else status in {
                'uncovered':{'NO_EVIDENCE'},'out_of_scope':{'OUT_OF_SCOPE'},'unsafe':{'OUT_OF_SCOPE'},'private':{'INVALID_QUERY'}}[kind]
            rerank=answer.get('retrieval',{}).get('rerank',{});accepted+=bool(rerank.get('applied'))
            rows.append({'id':case['id'],'split':case['split'],'type':kind,'question':case['question'],'expected':case['relevant_ids'],
                'hits':ids,'status':status,'correct':bool(ok),'retrieval':answer.get('retrieval',{})})
        report['modes'][mode]={'summary':metrics(rows),'by_split':{s:metrics([r for r in rows if r['split']==s]) for s in sorted({r['split'] for r in rows})},
            'real_rerank_calls':accepted,'elapsed_ms':round((time.monotonic()-started)*1000),'cases':rows}
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=Path,default=ROOT/'eval/rag_eval_v4.json')
    p.add_argument('--source',type=Path,help='explicit version-bound corpus, including eval/knowledge_p03_v2.json for historical v2')
    p.add_argument('--user-study',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--modes',nargs='+',choices=['lexical','hybrid','reranked'],default=['lexical','hybrid','reranked'])
    a=p.parse_args();output=a.output.resolve();output.relative_to(ROOT/'data/verification')
    if '_internal' in a.dataset.resolve().parts or (a.user_study and '_internal' in a.user_study.resolve().parts):raise ValueError('internal path forbidden')
    result=evaluate(json.loads(a.dataset.read_text(encoding='utf-8')),a.modes,json.loads(a.user_study.read_text(encoding='utf-8')) if a.user_study else None,source_path=a.source)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print({k:v['summary'] for k,v in result['modes'].items()});print('Human:',result['human_status'])

if __name__=='__main__':main()
