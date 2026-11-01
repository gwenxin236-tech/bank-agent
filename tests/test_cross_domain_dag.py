import unittest,tempfile,copy
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
class CrossDomainDagTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'dag.sqlite3',offline=True);self.old=self.rt.chat('给妈妈转1元');self.pid=self.old['plan']['plan_id']
 def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
 def propose(self):return self.rt.dynamic_dag('propose',self.pid,'demo-user','demo-device',mode='cross_domain')
 def adopt(self,p):return self.rt.dynamic_dag('adopt',self.pid,'demo-user','demo-device',proposal=p,action_token=self.old['plan_view']['action_token'])
 def test_cross_domain_report_condition_and_transfer_exactly_once(self):
  proposed=self.propose();self.assertTrue(proposed['ok'],proposed);tools=[n.get('tool') for n in proposed['proposal']['nodes']];self.assertIn('financial_health_report',tools);self.assertIn('check_funding_ready',tools)
  fresh=self.adopt(proposed['proposal']);self.assertTrue(fresh['ok'],fresh);self.assertEqual(self.rt.bank.daily_transfer_cents,0);self.assertFalse(self.rt.confirm(self.pid,self.old['plan_view']['action_token'])['ok'])
  done=self.rt.confirm(self.pid,fresh['plan_view']['action_token']);self.assertEqual(done['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,100)
  self.assertFalse(self.rt.confirm(self.pid,fresh['plan_view']['action_token'])['ok']);self.assertTrue(self.rt.audit.verify_trace(self.old['trace_id'])['valid'])
 def test_report_and_gate_must_precede_write_and_input_is_not_authority(self):
  proposed=self.propose();self.assertTrue(proposed['ok'],proposed)
  for mode in ['no_condition','no_source','approved_flag','extra_write']:
   p=copy.deepcopy(proposed['proposal']);gate=next(n for n in p['nodes'] if n.get('tool')=='check_funding_ready');write=next(n for n in p['nodes'] if n.get('step_ref')=='s3')
   if mode=='no_condition':write['depends_on']=[x for x in write['depends_on'] if x!=gate['id']]
   elif mode=='no_source':gate['depends_on']=[]
   elif mode=='approved_flag':gate['approved']=True
   else:p['nodes'].append({'id':'extra_payment','kind':'action','tool':'create_transfer','depends_on':[gate['id']]})
   self.assertFalse(self.adopt(p)['ok'],mode)
  self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_failed_or_forged_funding_evidence_blocks_without_debit(self):
  proposed=self.propose();self.assertTrue(proposed['ok'],proposed);fresh=self.adopt(proposed['proposal']);native=self.rt.bank.financial_health_report
  def bad(*args,**kwargs):
   result=native(*args,**kwargs);result.data['funding_plan']['target_cents']=999;return result
  with patch.object(self.rt.bank,'financial_health_report',side_effect=bad):out=self.rt.confirm(self.pid,fresh['plan_view']['action_token'])
  self.assertNotEqual(out['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_unknown_after_conditional_submission_is_not_retried(self):
  proposed=self.propose();self.assertTrue(proposed['ok'],proposed);fresh=self.adopt(proposed['proposal']);self.rt.bank.fault_injection='timeout'
  with patch.object(self.rt.bank,'transfer',wraps=self.rt.bank.transfer) as transfer:
   done=self.rt.confirm(self.pid,fresh['plan_view']['action_token']);self.assertIn(done['result']['status'],{'UNKNOWN','PROCESSING'})
   self.rt.reconcile_recovery_tasks();self.assertFalse(self.rt.confirm(self.pid,fresh['plan_view']['action_token'])['ok']);self.assertEqual(transfer.call_count,1)
 def test_cross_domain_scope_is_not_available_for_other_accounts(self):
  out=self.rt.chat('给妈妈转1元',actor_id='family-father-demo',device_id='father-device');pid=out['plan']['plan_id']
  proposed=self.rt.dynamic_dag('propose',pid,'family-father-demo','father-device',mode='cross_domain');self.assertFalse(proposed['ok'])
 def test_invalid_confirmed_money_is_unknown_not_silently_zero(self):
  self.rt.bank.confirmed_commitments=[{'id':'invalid','amount_cents':1.5,'status':'confirmed'}]
  result=self.rt.bank.confirmed_budget_commitments('demo-user');self.assertFalse(result.ok)
  out=self.rt.chat('给妈妈转1元');self.assertTrue(out['blocked']);self.assertEqual(out['plan']['preflight']['checks']['budget']['status'],'unknown');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
 def test_budget_change_after_finance_condition_is_rechecked_before_debit(self):
  p=self.propose();self.assertTrue(p['ok']);fresh=self.adopt(p['proposal']);native=self.rt.bank.financial_health_report
  def changed(*args,**kwargs):
   result=native(*args,**kwargs);self.rt.bank.confirmed_commitments=[{'id':'urgent','amount_cents':40000000,'status':'confirmed'}];return result
  with patch.object(self.rt.bank,'financial_health_report',side_effect=changed):out=self.rt.confirm(self.pid,fresh['plan_view']['action_token'])
  self.assertNotEqual(out['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
if __name__=='__main__':unittest.main()
