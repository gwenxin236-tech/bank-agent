import tempfile,unittest,concurrent.futures
from pathlib import Path
import banking_agent as ba
class ServiceSecurityTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'db.sqlite3',offline=True)
 def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
 def test_request_id_cannot_rebind_claim_and_source_fields_are_not_authority(self):
  tid=self.rt.bank.recent_transactions().data['items'][0]['id'];task=self.rt.service_tasks.dispute(tid,'not_authorized','same-key','demo-user','demo-device')
  with self.assertRaises(ValueError):self.rt.service_tasks.dispute(tid,'refund_pending','same-key','demo-user','demo-device')
  for value in [True,{},'approved','source_'+('0'*24)]:
   with self.assertRaises(ValueError):self.rt.sources.get(value,'demo-user','demo-device')
  self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_parallel_handoff_creates_one_local_case_without_bank_write(self):
  tid=self.rt.bank.recent_transactions().data['items'][0]['id'];task=self.rt.service_tasks.dispute(tid,'not_authorized','parallel','demo-user','demo-device')
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(lambda _:self.rt.service_tasks.handoff(task['task_id'],'demo-user','demo-device'),range(8)))
  self.assertEqual(len({x['local_case_id'] for x in rows}),1);self.assertEqual(self.rt.audit.db.execute('SELECT count(*) FROM local_handoff_cases').fetchone()[0],1);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_old_owned_transaction_remains_queryable_after_recent_page_changes(self):
  tid=self.rt.bank.recent_transactions().data['items'][-1]['id'];task=self.rt.service_tasks.dispute(tid,'not_authorized','older','demo-user','demo-device')
  self.rt.bank.transactions=[{'id':'new-'+str(i),'amount_cents':-1,'merchant':'合成新交易','actor_id':'demo-user'} for i in range(20)]+self.rt.bank.transactions
  updated=self.rt.service_tasks.recheck(task['task_id'],task['version'],'demo-user','demo-device');self.assertEqual(updated['bank_fact']['transaction_id'],tid)
 def test_material_reference_follows_handoff_without_authority(self):
  tid=self.rt.bank.recent_transactions().data['items'][0]['id'];task=self.rt.service_tasks.dispute(tid,'not_authorized','material','demo-user','demo-device')
  source=self.rt.sources.record('voice','draft','trace-material','demo-user','demo-device',{'upload_sha256':'a'*64,'pcm_sha256':'b'*64,'normalized_wav_sha256':'c'*64})
  self.rt.service_tasks.attach_source(task['task_id'],source['source_id'],task['version'],'demo-user','demo-device');self.rt.service_tasks.handoff(task['task_id'],'demo-user','demo-device');card=self.rt.handoffs.inbox()[0]['card']
  self.assertIn(source['source_id'],card['business_identifiers']['source_ids']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
if __name__=='__main__':unittest.main()
