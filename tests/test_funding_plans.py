import unittest,tempfile
from pathlib import Path
import banking_agent as ba
from ledger_views import build_funding_plan,build_ledger_views
class FundingTests(unittest.TestCase):
 def test_current_money_reserve_commitments_and_expected_are_separate(self):
  p=build_funding_plan(available_cents=10000,target_cents=9000,reserve_cents=2000,commitments=[{'id':'rent','amount_cents':1000,'status':'confirmed'}],sources=[{'id':'salary','amount_cents':5000,'status':'expected','available_at':'2099-01-01T00:00:00+00:00'}])
  self.assertEqual(p['spendable_now_cents'],7000);self.assertEqual(p['shortfall_cents'],2000);self.assertEqual(p['status'],'shortfall');self.assertFalse(p['business_executed'])
 def test_duplicates_conflicts_and_missing_balance_fail_closed(self):
  args=dict(available_cents=10000,target_cents=1000,reserve_cents=1000)
  same=[{'id':'x','amount_cents':100,'status':'confirmed'}]*2
  self.assertEqual(build_funding_plan(**args,commitments=same)['confirmed_expenses_cents'],100)
  same[1]={'id':'x','amount_cents':200,'status':'confirmed'}
  self.assertEqual(build_funding_plan(**args,commitments=same)['status'],'needs_review')
  self.assertIsNone(build_funding_plan(available_cents=None,target_cents=100,reserve_cents=0)['shortfall_cents'])
 def test_deadlines_and_input_types(self):
  self.assertEqual(build_funding_plan(available_cents=10000,target_cents=100,reserve_cents=0,deadline='2000-01-01T00:00:00+00:00')['status'],'deadline_expired')
  for kwargs in [dict(available_cents=True),dict(target_cents=1.5),dict(reserve_cents=-1),dict(deadline='2026-11-01')]:
   args=dict(available_cents=1000,target_cents=100,reserve_cents=0);args.update(kwargs)
   with self.assertRaises((ValueError,TypeError)):build_funding_plan(**args)
 def test_failed_reversed_and_posted_inflight_duplicates_not_double_counted(self):
  views=build_ledger_views([{'amount_cents':-100,'status':'failed'},{'amount_cents':-200,'status':'reversed'},{'canonical_id':'x','amount_cents':-300,'status':'processing'},{'canonical_id':'x','amount_cents':-300,'status':'succeeded'}])
  self.assertEqual(views['cash_flow_view']['outflow_cents'],300);self.assertEqual(views['funds_status']['processing_cents'],0)
 def test_main_chain_returns_plan_and_never_moves_funds(self):
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    out=rt.chat('2030-11-01前准备1000元，至少保留2000元');self.assertEqual(out['plan']['intent'],'financial_health')
    data=out['result']['results'][0]['data'];self.assertEqual(data['funding_plan']['target_cents'],100000);self.assertGreaterEqual(data['funding_plan']['reserve_cents'],200000)
    self.assertIn('不代表到账承诺',out['result_view']['user_message']);self.assertEqual(rt.bank.daily_transfer_cents,0)
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
if __name__=='__main__':unittest.main()
