import tempfile,unittest
from pathlib import Path
import banking_agent as ba
class PrototypeJourneys(unittest.TestCase):
 def test_a_limit_blocked_handoff_then_explicit_recovery(self):
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    self.assertTrue(rt.bank.transfer(5000000,'妈妈','隔离验收额度夹具','idem:n11seed:create_transfer').ok)
    out=rt.chat('给妈妈转80000元');self.assertTrue(out['blocked']);pid=out['plan']['plan_id']
    handoff=rt.service_tasks.payment_handoff(pid,'demo-user','demo-device');self.assertEqual(handoff['local_case']['status'],'LOCAL_QUEUED');self.assertFalse(handoff['business_executed'])
    self.assertEqual(rt.bank.daily_transfer_cents,5000000)
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
 def test_b_user_claim_is_not_denied_by_transaction_existence(self):
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    txn=rt.bank.recent_transactions().data['items'][0]['id'];task=rt.service_tasks.dispute(txn,'not_authorized','prototype-b','demo-user','demo-device')
    self.assertEqual(task['bank_fact']['transaction_id'],txn);self.assertEqual(task['user_claim'],'not_authorized');self.assertIsNone(task['official_case_id'])
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
 def test_c_future_assets_not_counted_as_current_funds(self):
  from ledger_views import build_funding_plan
  plan=build_funding_plan(available_cents=3000000,target_cents=4000000,reserve_cents=1000000,deadline='2030-11-01T00:00:00+08:00',sources=[{'amount_cents':2000000,'status':'expected'}])
  self.assertEqual(plan['shortfall_cents'],2000000);self.assertEqual(plan['future_unavailable_cents'],2000000);self.assertFalse(plan['business_executed'])
if __name__=='__main__':unittest.main()
