"""N03 business headers are durable views, never execution authority."""
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch
import banking_agent as ba

class BusinessTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'bank.sqlite3'
        self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
    def close(self):
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
    def tearDown(self):self.close();self.tmp.cleanup()
    def restart(self):self.close();self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
    def test_blocked_task_survives_restart_without_restoring_permission(self):
        self.rt.bank.confirmed_commitments=[{'id':'rent','name':'房租','amount_cents':40000000,'status':'confirmed'}]
        response=self.rt.chat('给妈妈转1元');task=response['business_task'];tid=task['task_id']
        self.assertEqual(task['status'],'blocked');self.assertEqual(task['goal']['amount_cents'],100)
        self.assertIn('budget',[x['check'] for x in task['blockers']]);self.assertNotIn(tid,self.rt.pending)
        self.restart();saved=self.rt.business_tasks.get(tid,'demo-user','demo-device')
        self.assertEqual(saved['status'],'blocked');self.assertEqual(saved['goal'],task['goal'])
        self.assertNotIn(tid,self.rt.pending);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_pending_and_completed_tasks_have_provenance_receipt_and_timeline(self):
        response=self.rt.chat('给妈妈转1元');task=response['business_task'];tid=task['task_id']
        self.assertEqual(task['status'],'waiting_confirmation');self.assertTrue(task['facts'])
        self.assertTrue(all(x['source'] and x['observed_at'] for x in task['facts']))
        done=self.rt.confirm(tid,response['plan_view']['action_token'])
        task=self.rt.business_tasks.get(tid,'demo-user','demo-device')
        self.assertEqual(task['status'],'succeeded');self.assertTrue(task['receipts'])
        self.assertEqual(self.rt.bank.daily_transfer_cents,100)
        self.assertIn('processing',[e['status'] for e in task['timeline']])
        self.restart();self.assertEqual(self.rt.business_tasks.get(tid,'demo-user','demo-device')['status'],'succeeded')
    def test_wrong_actor_or_device_cannot_enumerate_or_read(self):
        task=self.rt.chat('给妈妈转1元')['business_task'];tid=task['task_id']
        self.assertIsNone(self.rt.business_tasks.get(tid,'other','demo-device'))
        self.assertIsNone(self.rt.business_tasks.get(tid,'demo-user','other-device'))
        self.assertEqual(self.rt.business_tasks.list('other','demo-device'),[])
        self.assertEqual(self.rt.business_tasks.list('demo-user','other-device'),[])
    def test_public_header_does_not_contain_execution_credentials_or_raw_input(self):
        response=self.rt.chat('给妈妈转1元');task=response['business_task'];raw=json.dumps(task)
        self.assertNotIn(response['plan']['consent_token'],raw)
        for field in ('consent_token','action_token','credential_id','user_input','password','otp'):
            self.assertNotIn(field,raw)
        self.assertEqual(task['authorization']['meaning'],'requirements_and_records_only')
        with patch.object(self.rt.bank,'transfer',side_effect=AssertionError('read caused debit')):
            self.rt.business_tasks.list('demo-user','demo-device')
            self.rt.business_tasks.get(task['task_id'],'demo-user','demo-device')
    def test_clarification_task_is_saved_without_an_executable_plan(self):
        response=self.rt.chat('给妈妈转账');task=response['business_task']
        self.assertEqual(task['status'],'needs_clarification');self.assertIsNone(task['goal']['amount_cents'])
        self.restart();self.assertFalse(self.rt.pending)
        self.assertEqual(len(self.rt.business_tasks.list('demo-user','demo-device')),1)
    def test_unknown_facts_remain_unknown_in_header(self):
        with patch.object(self.rt.bank,'balance',return_value=ba.ToolResult(False,'get_balance')):
            response=self.rt.chat('给妈妈转1元')
        facts=response['business_task']['facts'];funds=next(x for x in facts if x['check']=='available_funds')
        self.assertEqual(funds['status'],'unknown');self.assertNotIn('available_cents',funds['evidence'])
        self.assertEqual(response['business_task']['status'],'blocked')

    def test_rejected_identity_mutation_does_not_overwrite_durable_owner(self):
        response=self.rt.chat('给妈妈转1元');tid=response['plan']['plan_id'];token=response['plan_view']['action_token']
        plan=self.rt.pending[tid];plan.actor_id='forged-owner'
        try:
            self.assertFalse(self.rt.confirm(tid,token)['ok'])
            self.assertEqual(self.rt.recovery_store.get(tid)[1]['plan']['actor_id'],'demo-user')
            self.assertIsNotNone(self.rt.business_tasks.get(tid,'demo-user','demo-device'))
            self.assertIsNone(self.rt.business_tasks.get(tid,'forged-owner','demo-device'))
            self.assertEqual(self.rt.bank.daily_transfer_cents,0)
        finally:plan.actor_id='demo-user'


if __name__=='__main__':unittest.main()
