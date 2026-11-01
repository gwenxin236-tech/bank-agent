import unittest,tempfile
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import banking_agent as ba
from domain_snapshots import policy_snapshot
from supervisor import SupervisorError
class OrchestrationTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'bank.sqlite3',offline=True)
 def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
 def test_four_domain_evidence_shown_on_main_chain(self):
  out=self.rt.chat('给妈妈转1元');reviews=out['plan_view']['domain_reviews'];self.assertEqual(set(reviews),{'payment','antifraud','finance_health'})
  for data in reviews.values():self.assertEqual(data['authority'],'advisory_only');self.assertIn('fact_count',data)
  report=self.rt.chat('财务健康');self.assertIn('finance_health',report['plan_view']['domain_reviews'])
 def test_expired_snapshot_rejected_without_majority_voting(self):
  out=self.rt.chat('给妈妈转1元');plan=self.rt.pending[out['plan']['plan_id']];ctx=self.rt._domain_context(plan)
  snapshot=policy_snapshot(ctx,plan.risk_assessment);old=replace(snapshot,captured_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
  with self.assertRaises(SupervisorError):self.rt.supervisor.observe(ctx,'antifraud',old)
 def test_domain_failure_preserves_real_read_result_and_marks_review(self):
  with patch.object(self.rt.supervisor,'observe',side_effect=SupervisorError('unavailable')):out=self.rt.chat('财务健康')
  self.assertEqual(out['result']['status'],'SUCCESS');self.assertEqual(out['plan_view']['domain_reviews']['finance_health']['status'],'needs_review');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_family_preflight_does_not_inherit_demo_user_commitments(self):
  self.assertEqual(self.rt.bank.confirmed_budget_commitments('family-father-demo').data['items'],[])
  self.assertTrue(self.rt.bank.confirmed_budget_commitments('demo-user').data['items'])
 def test_payment_preflight_timestamp_is_checked(self):
  from transaction_agent import PaymentSnapshot
  from domain_agent_contract import thaw
  out=self.rt.chat('给妈妈转1元');plan=self.rt.pending[out['plan']['plan_id']];ctx=self.rt._domain_context(plan)
  report=thaw(plan.preflight);report['generated_at']='2000-01-01T00:00:00+00:00'
  with self.assertRaises(SupervisorError):self.rt.supervisor._expected(ctx,'payment',PaymentSnapshot.from_preflight(ctx,report))
if __name__=='__main__':unittest.main()
