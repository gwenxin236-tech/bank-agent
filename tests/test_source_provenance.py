import unittest,tempfile,json,hashlib
from pathlib import Path
from dataclasses import replace
import banking_agent as ba
class SourceProvenanceTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'bank.sqlite3',offline=True)
 def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
 def test_rag_sources_link_to_local_task_without_authority(self):
  answer=self.rt.chat('知识问答：什么是冷静期');plan=answer['plan'];sources=self.rt.sources.for_task(plan['plan_id'],'demo-user','demo-device');self.assertEqual(sources[0]['kind'],'rag');self.assertTrue(sources[0]['evidence']['citations'])
  txn=self.rt.bank.recent_transactions().data['items'][0]['id'];task=self.rt.service_tasks.dispute(txn,'not_authorized','source-case','demo-user','demo-device');linked=self.rt.service_tasks.attach_source(task['task_id'],sources[0]['source_id'],task['version'],'demo-user','demo-device');self.assertEqual(linked['sources'][0]['authority'],'reference_only');self.assertEqual(linked['refund_status'],'UNVERIFIED');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_foreign_source_and_stale_knowledge_cannot_be_linked(self):
  out=self.rt.chat('知识问答：什么是冷静期');source=self.rt.sources.for_task(out['plan']['plan_id'],'demo-user','demo-device')[0]
  self.assertEqual(self.rt.sources.for_task(out['plan']['plan_id'],'other','demo-device'),[])
  with self.assertRaises(PermissionError):self.rt.sources.validate(source['source_id'],'other','demo-device')
  self.rt.knowledge.available=False
  with self.assertRaises(ValueError):self.rt.sources.validate(source['source_id'],'demo-user','demo-device')
 def test_metadata_never_retains_raw_material_or_credentials(self):
  source=self.rt.sources.record('voice','task','trace','demo-user','demo-device',{'upload_sha256':'a'*64,'pcm_sha256':'b'*64,'normalized_wav_sha256':'c'*64,'source_trace_id':'trace_audio','draft_changed':True})
  raw=json.dumps(source);self.assertNotIn('audio_bytes',raw);self.assertNotIn('action_token',raw);self.assertEqual(source['authority'],'reference_only')
  with self.assertRaises(ValueError):self.rt.sources.record('voice','task2','trace2','demo-user','demo-device',{'approved':True})
 def test_ocr_review_preserves_positions_and_field_digest_but_not_image(self):
  self.rt.ocr._drafts['fixture']={'owner':'demo-user','device':'demo-device','expires':self.rt.ocr.clock()+300,'sha256':'a'*64,'flags':[],'source_trace_id':'trace_ocr','field_evidence':{'amount':[{'line':0,'confidence_milli':990,'box':[[1,1],[2,1],[2,2],[1,2]]}]}}
  reviewed=self.rt.ocr.review('fixture','demo-user','demo-device',recipient='妈妈',amount='1.00',reviewed=True);e=self.rt.ocr.consume_review(reviewed['review_token'],'demo-user','demo-device');self.assertEqual(len(e['reviewed_fields_sha256']),64);self.assertEqual(e['field_evidence']['amount'][0]['line'],0)
  with self.assertRaises(PermissionError):self.rt.ocr.consume_review(reviewed['review_token'],'demo-user','demo-device')
 def test_family_knowledge_requires_current_explicit_read_consent(self):
  store=self.rt.bank.guardianships;store.require_explicit_consent=True
  ctx={'grant_id':'grant-daughter-father','guardian_id':'family-daughter-demo','guardian_device_id':'daughter-device','subject_id':'family-father-demo','mode':'assist','consent_version':0}
  self.assertFalse(self.rt.policy.classify('knowledge_query',{'_family':ctx},'family-father-demo')['authz_allowed'])
  store.set_consent(ctx['grant_id'],'family-father-demo',0,{'read':True,'notify':False,'assist':False});ctx['consent_version']=1
  self.assertTrue(self.rt.policy.classify('knowledge_query',{'_family':ctx},'family-father-demo')['authz_allowed'])
  store.set_consent(ctx['grant_id'],'family-father-demo',1,{'read':False,'notify':False,'assist':False})
  self.assertFalse(self.rt.policy.classify('knowledge_query',{'_family':ctx},'family-father-demo')['authz_allowed'])
if __name__=='__main__':unittest.main()
