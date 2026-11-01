import unittest,tempfile,json
from pathlib import Path
from prototype_load_test import run_profile,validate_profile
ROOT=Path(__file__).resolve().parents[1]
class PrototypePerformanceTests(unittest.TestCase):
 def test_bounded_profile_rejects_unsafe_load_before_any_server(self):
  plan=json.loads((ROOT/'config/prototype_performance_plan.json').read_text());plan['concurrency']=[999]
  with self.assertRaises(ValueError):validate_profile(plan)
 def test_small_real_http_profile_checks_ledger_and_all_workflows(self):
  plan=json.loads((ROOT/'config/prototype_performance_plan.json').read_text());plan.update(concurrency=[1,2],stage_seconds=1,stage_operation_cap=20,soak_seconds=1,soak_operation_cap=20)
  with tempfile.TemporaryDirectory() as folder:result=run_profile(plan,Path(folder))
  self.assertTrue(result['correctness']['exact_ledger_match']);self.assertTrue(result['correctness']['audit_whole_prefix_valid']);self.assertEqual(result['http']['errors'],0);self.assertTrue(all(x['errors']==0 for x in result['stages']));self.assertEqual(result['cloud_calls'],0)
 def test_existing_audit_chain_migrates_to_indexed_append_without_changing_events(self):
  import banking_agent as ba
  queries=["SELECT seq,current_hash,event_hash FROM audit_events WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1", "SELECT user_id,device_id,actor FROM audit_events WHERE trace_id=? ORDER BY seq DESC,id DESC LIMIT 1"]
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'old.sqlite3';audit=ba.AuditStore(path)
   try:
    for i in range(80):audit.write('target' if i%2 else 'other','observed',{'ordinal':i},actor='demo-user')
    before=[tuple(r) for r in audit.db.execute('SELECT * FROM audit_events ORDER BY id')]
    audit.db.execute('DROP INDEX IF EXISTS audit_seq_lookup');audit.db.execute('DROP INDEX IF EXISTS audit_trace_lookup');audit.db.commit()
   finally:audit.db.close()
   audit=ba.AuditStore(path)
   try:
    self.assertEqual(before,[tuple(r) for r in audit.db.execute('SELECT * FROM audit_events ORDER BY id')])
    self.assertTrue(audit.verify_trace('target')['valid'])
    for sql in queries:
     detail=' '.join(row[3] for row in audit.db.execute('EXPLAIN QUERY PLAN '+sql,('target',) if '?' in sql else ()))
     self.assertIn('USING',detail);self.assertNotIn('TEMP B-TREE',detail)
    audit.write('target','appended',{},actor='demo-user');self.assertTrue(audit.verify_trace('target')['valid'])
    audit.db.execute("UPDATE audit_events SET payload_json='{}' WHERE id=1");audit.db.commit()
    self.assertFalse(audit.verify_trace('target')['valid'])
   finally:audit.db.close()
if __name__=='__main__':unittest.main()
