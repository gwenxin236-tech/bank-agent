#!/usr/bin/env python3
"""Anonymized, attested manual study import; never executes participant requests."""
import argparse,json,re,sys,hashlib
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
from knowledge_service import _private
OUTCOMES={'completed','blocked','needs_review','local_handoff'}
def validate_study(data):
 if data.get('schema')!=1 or not isinstance(data.get('samples'),list) or len(data['samples'])>500:raise ValueError('Invalid study')
 if not data['samples']:return {'status':'PENDING','count':0,'correct':0,'failures':[],'identity_verified':False,'scope':'No real participant samples supplied'}
 if data.get('provenance')!='real_people' or data.get('consent') is not True:raise ValueError('Real participant provenance and consent required')
 rows=[]
 for i,row in enumerate(data['samples']):
  person=row.get('participant_id','');reviewers=row.get('reviewers',[]);question=row.get('question','')
  if not re.fullmatch('p_[A-Za-z0-9]{4,16}',person) or not isinstance(reviewers,list) or len(set(reviewers))<2 or person in reviewers or any(not isinstance(x,str) or not re.fullmatch('p_[A-Za-z0-9]{4,16}',x) for x in reviewers):raise ValueError('Independent pseudonymous reviewers required')
  if not isinstance(question,str) or not 0<len(question)<=400 or _private(question):raise ValueError('Private or invalid input')
  if row.get('scenario') not in {'prototype_a','prototype_b','prototype_c'} or row.get('modality') not in {'text','ocr','audio','screen_reader'} or row.get('expected_outcome') not in OUTCOMES or row.get('observed_outcome') not in OUTCOMES:raise ValueError('Invalid label')
  if datetime.fromisoformat(row.get('submitted_at','')).tzinfo is None:raise ValueError('Timezone required')
  if row['modality']!='text' and not re.fullmatch('[0-9a-f]{64}',row.get('artifact_sha256','')):raise ValueError('Anonymized media evidence hash required')
  rows.append({'sample_id':i+1,'participant_id':person,'modality':row['modality'],'scenario':row['scenario'],'correct':row['expected_outcome']==row['observed_outcome']})
 return {'status':'READY_ATTESTED','count':len(rows),'correct':sum(x['correct'] for x in rows),'failures':[x for x in rows if not x['correct']],'rows':rows,'identity_verified':False,'scope':'Owner-supplied provenance and annotations; not independently verified participants or hardware evidence'}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();source=a.input.resolve();out=a.output.resolve()
 if '_internal' in source.parts or not any(source.is_relative_to(ROOT/x) for x in ('eval','data/verification')) or not out.is_relative_to(ROOT/'data/verification') or out.exists():raise ValueError('Owned input and fresh output required')
 raw=source.read_bytes();result=validate_study(json.loads(raw.decode('utf-8-sig')));result['source_sha256']=hashlib.sha256(raw).hexdigest();out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':result['status'],'count':result['count']}))
if __name__=='__main__':main()
