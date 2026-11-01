import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
class ServiceTaskTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'bank.sqlite3';self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
 def close(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
 def tearDown(self):self.close();self.tmp.cleanup()
 def transaction(self):return self.rt.bank.recent_transactions().data['items'][0]['id']
 def dispute(self,key='case1'):return self.rt.service_tasks.dispute(self.transaction(),'not_authorized',key,'demo-user','demo-device')
 def test_claim_and_bank_facts_persist_separately_without_funding_effect(self):
  old=self.rt.bank.balance_cents;task=self.dispute();self.assertEqual(task['user_claim'],'not_authorized');self.assertIsNone(task['official_case_id']);self.assertEqual(task['refund_status'],'UNVERIFIED');self.close();self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
  saved=self.rt.service_tasks.get(task['task_id'],'demo-user','demo-device');self.assertEqual(saved['bank_fact'],task['bank_fact']);self.assertEqual(self.rt.bank.balance_cents,old)
 def test_idempotent_request_and_foreign_access(self):
  task=self.dispute();self.assertEqual(self.dispute()['task_id'],task['task_id'])
  with self.assertRaises(PermissionError):self.rt.service_tasks.get(task['task_id'],'other','demo-device')
  self.assertIsNone(self.rt.service_tasks.get(task['task_id'],'demo-user','other'))
  with self.assertRaises(PermissionError):self.rt.service_tasks.dispute('foreign-tx','not_authorized','new','demo-user','demo-device')
 def test_local_case_closure_never_proves_refund_or_payment(self):
  task=self.dispute();out=self.rt.service_tasks.handoff(task['task_id'],'demo-user','demo-device');case=out['local_case']
  self.rt.handoffs.transition(case['case_id'],'claim','local-operator','op-device');self.rt.handoffs.transition(case['case_id'],'resolve','local-operator','op-device','GUIDANCE_PROVIDED')
  saved=self.rt.service_tasks.get(task['task_id'],'demo-user','demo-device');self.assertEqual(saved['local_case']['status'],'LOCAL_CLOSED');self.assertEqual(saved['refund_status'],'UNVERIFIED');self.assertEqual(saved['business_outcome'],'BANK_REVIEW_REQUIRED');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_funding_goal_preserves_deadline_no_borrow_and_rechecks_by_version(self):
  task=self.rt.service_tasks.funding(10000,200000,'2030-11-01T00:00:00+08:00','fund1','demo-user','demo-device');self.assertTrue(task['constraints']['no_borrow']);self.assertEqual(task['funding_plan']['target_cents'],10000)
  self.rt.bank.confirmed_commitments=[{'id':'rent','amount_cents':40000000,'status':'confirmed'}]
  updated=self.rt.service_tasks.recheck(task['task_id'],task['version'],'demo-user','demo-device');self.assertEqual(updated['funding_plan']['status'],'shortfall')
  with self.assertRaises(ValueError):self.rt.service_tasks.recheck(task['task_id'],task['version'],'demo-user','demo-device')
 def test_invalid_money_deadline_and_reason_rejected(self):
  for value in [True,1.2,-1]:
   with self.assertRaises(ValueError):self.rt.service_tasks.funding(value,0,'2030-11-01T00:00:00+08:00','f','demo-user','demo-device')
  with self.assertRaises(ValueError):self.rt.service_tasks.funding(100,0,'2030-11-01','f','demo-user','demo-device')
  with self.assertRaises(ValueError):self.rt.service_tasks.dispute(self.transaction(),'bank_approved_refund','f','demo-user','demo-device')
if __name__=='__main__':unittest.main()
