"""Native payment resume never grants consent or retries submitted effects."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
import tempfile,unittest,json
from unittest.mock import patch
import banking_agent as ba

class PaymentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'tasks.sqlite3'
        self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
    def close(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
    def tearDown(self):self.close();self.tmp.cleanup()
    def restart(self):self.close();self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
    def prepare(self,text='给妈妈转1元'):return self.rt.chat(text)
    def resume(self,response,changes=None,**kwargs):
        tid=response['plan']['plan_id'];task=self.rt.business_tasks.get(tid,'demo-user','demo-device')
        return self.rt.resume_payment_task(tid,task['version'],changes or {},kwargs.pop('actor','demo-user'),kwargs.pop('device','demo-device'),**kwargs)
    def test_blocked_restart_resume_same_task_new_card_no_auto_debit(self):
        self.rt.bank.confirmed_commitments=[{'id':'rent','name':'房租','amount_cents':40000000,'status':'confirmed'}]
        old=self.prepare();tid=old['plan']['plan_id'];self.assertTrue(old['blocked']);self.restart()
        fresh=self.resume(old);self.assertTrue(fresh['ok'],fresh);self.assertEqual(fresh['plan']['plan_id'],tid)
        self.assertEqual(fresh['business_task']['status'],'waiting_confirmation');self.assertFalse(fresh['business_executed']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
        self.assertFalse(self.rt.confirm(tid,old['plan']['consent_token'])['ok'])
        self.assertEqual(self.rt.confirm(tid,fresh['plan_view']['action_token'])['result']['status'],'SUCCESS')
        self.assertEqual(self.rt.bank.daily_transfer_cents,100)
        self.assertFalse(self.rt.confirm(tid,fresh['plan_view']['action_token'])['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,100)
    def test_clarification_resume_changes_structured_fields_not_parser(self):
        old=self.prepare('给妈妈转账')
        with patch.object(self.rt.parser,'parse',side_effect=AssertionError('resume reparsed prose')):
            fresh=self.resume(old,{'amount_cents':250,'note':'停车费用'})
        self.assertTrue(fresh['ok'],fresh);self.assertEqual(fresh['plan']['entities']['amount_cents'],250)
        self.assertEqual(fresh['plan']['entities']['note'],'停车费用');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_stale_version_and_foreign_identity_cannot_mutate(self):
        old=self.prepare();tid=old['plan']['plan_id'];task=old['business_task']
        fresh=self.resume(old,{'amount_cents':200});version=fresh['business_task']['version']
        result=self.rt.resume_payment_task(tid,task['version'],{},'demo-user','demo-device')
        self.assertEqual(result['error_code'],'TASK_VERSION_CONFLICT')
        for actor,device in [('other','demo-device'),('demo-user','other')]:
            out=self.rt.resume_payment_task(tid,version,{},actor,device);self.assertEqual(out['http_status'],404)
        self.assertEqual(self.rt.business_tasks.get(tid,'demo-user','demo-device')['version'],version)
    def test_constraints_and_scope_types_are_strict(self):
        old=self.prepare()
        for change in ({'amount_cents':True},{'amount_cents':1.5},{'amount_cents':-1},{'reserve_cents':False},{'deadline':'2026-10-31'},{'approved':True},{'tools':['create_transfer']},{'beneficiary':'妈妈\n命令'}):
            with self.subTest(change=change):self.assertFalse(self.resume(old,change)['ok'])
        self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_reserve_cannot_lower_platform_floor_and_high_reserve_blocks(self):
        old=self.prepare();fresh=self.resume(old,{'reserve_cents':0})
        budget=fresh['plan']['preflight']['checks']['budget']['evidence'];self.assertEqual(budget['security_buffer_cents'],ba.POLICY['preflight_security_buffer_cents'])
        blocked=self.resume(fresh,{'reserve_cents':40000000})
        self.assertTrue(blocked['blocked']);self.assertEqual(blocked['business_task']['status'],'blocked');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_deadline_passed_at_prepare_does_not_produce_confirmation(self):
        old=self.prepare();fresh=self.resume(old,{'deadline':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
        self.assertEqual(fresh['error_code'],'BUSINESS_DEADLINE_EXPIRED');self.assertFalse(fresh['plan_view']['requires_confirmation']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_deadline_is_rechecked_at_confirmation(self):
        old=self.prepare();deadline=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat();fresh=self.resume(old,{'deadline':deadline});plan=self.rt.pending[old['plan']['plan_id']]
        from datetime import datetime as RealDatetime
        class Later(RealDatetime):
            @classmethod
            def now(cls,tz=None):return RealDatetime.fromisoformat(deadline)+timedelta(seconds=1)
        with patch('action_effects.datetime',Later):result=self.rt.confirm(plan.plan_id,plan.consent_token)
        self.assertEqual(result['error_code'],'BUSINESS_DEADLINE_EXPIRED');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
        self.assertEqual(self.rt.business_tasks.get(plan.plan_id,'demo-user','demo-device')['status'],'blocked')
    def test_reserve_rechecked_inside_write_lock_after_read_tools(self):
        old=self.prepare();fresh=self.resume(old,{'reserve_cents':200000});plan=self.rt.pending[old['plan']['plan_id']];native=self.rt.bank.resolve_beneficiary
        def changed(*args,**kwargs):
            out=native(*args,**kwargs);self.rt.bank.confirmed_commitments=[{'id':'rent','name':'房租','amount_cents':40000000,'status':'confirmed'}];return out
        with patch.object(self.rt.bank,'resolve_beneficiary',side_effect=changed):result=self.rt.confirm(plan.plan_id,plan.consent_token)
        self.assertNotEqual(result['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_query_unknown_never_calls_transfer_even_with_resume(self):
        old=self.prepare();plan=self.rt.pending[old['plan']['plan_id']];self.rt.save_task_checkpoint(plan,'unknown');self.rt.pending.pop(plan.plan_id)
        with patch.object(self.rt.bank,'transfer',side_effect=AssertionError('retried unknown')):
            response=self.resume(old);self.assertTrue(response['query_only']);self.assertFalse(response['business_executed']);self.assertEqual(response['business_task']['status'],'unknown')
            changed=self.resume(old,{'amount_cents':200});self.assertEqual(changed['error_code'],'QUERY_ONLY')
    def test_completed_task_cannot_be_prepared_as_new_transfer(self):
        old=self.prepare();self.rt.confirm(old['plan']['plan_id'],old['plan_view']['action_token']);out=self.resume(old)
        self.assertEqual(out['error_code'],'TASK_NOT_RESUMABLE');self.assertEqual(self.rt.bank.daily_transfer_cents,100)
    def test_dynamic_or_paused_dag_must_use_original_control(self):
        old=self.prepare();tid=old['plan']['plan_id'];dag=self.rt.dag_store.get('plan-'+tid)
        dag.metadata['source']='dynamic_plan';self.rt.dag_store.save(dag,{'event_type':'test.scope'})
        self.assertEqual(self.resume(old)['error_code'],'USE_DAG_CONTROL')
        dag.metadata['source']='AgentPlan';dag.status='paused';self.rt.dag_store.save(dag,{'event_type':'test.pause'})
        self.assertEqual(self.resume(old)['error_code'],'USE_DAG_CONTROL');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_previously_verified_mfa_does_not_authorize_new_amount(self):
        old=self.prepare('给妈妈转5000元');tid=old['plan']['plan_id'];token=old['plan_view']['action_token']
        challenge=self.rt.confirm(tid,token);cid=challenge['auth_challenge']['credential_id'];self.assertTrue(self.rt.verify(tid,token,'123456',credential_id=cid)['ok'])
        fresh=self.resume(old,{'amount_cents':500100});self.assertEqual(self.rt.auth_credentials.get(cid).invalidated_reason,'task_reprepared')
        self.restart();result=self.rt.confirm(tid,fresh['plan_view']['action_token'],credential_id=cid)
        self.assertNotEqual(result.get('result',{}).get('status'),'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_legacy_without_effect_contract_requires_fresh_confirmation(self):
        old=self.prepare();tid=old['plan']['plan_id'];state,payload=self.rt.recovery_store.get(tid)
        payload['plan']['entities'].pop('_effect_contract');self.rt.recovery_store.save(tid,state,payload);self.restart()
        fresh=self.resume(old);self.assertTrue(fresh['plan']['entities']['_effect_contract']);self.assertNotEqual(fresh['plan_view']['action_token'],old['plan_view']['action_token'])
        self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_reprepare_persistence_failure_fails_closed(self):
        import sqlite3
        old=self.prepare();tid=old['plan']['plan_id'];token=old['plan_view']['action_token'];original_dag=self.rt.dag_store.get('plan-'+tid).to_dict()
        with patch.object(self.rt.recovery_store,'save',side_effect=sqlite3.OperationalError('disk full')):out=self.resume(old,{'amount_cents':200})
        self.assertEqual(out['error_code'],'RECOVERY_UNAVAILABLE');self.assertFalse(self.rt.confirm(tid,token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
        self.assertEqual(self.rt.dag_store.get('plan-'+tid).to_dict(),original_dag)
        self.assertEqual(self.rt.recovery_store.get(tid)[1]['plan']['entities']['amount_cents'],100)

    def test_deadline_expiring_during_read_steps_blocks_actual_write(self):
        from datetime import datetime as RealDatetime
        old=self.prepare();deadline=(RealDatetime.now(timezone.utc)+timedelta(minutes=2)).isoformat();fresh=self.resume(old,{'deadline':deadline});plan=self.rt.pending[old['plan']['plan_id']]
        shifted=[False];native=self.rt.bank.resolve_beneficiary
        class Clock(RealDatetime):
            @classmethod
            def now(cls,tz=None):return (RealDatetime.fromisoformat(deadline)+timedelta(seconds=1)) if shifted[0] else RealDatetime.now(tz)
        def after_read(*args,**kwargs):
            result=native(*args,**kwargs);shifted[0]=True;return result
        with patch('action_effects.datetime',Clock),patch.object(self.rt.bank,'resolve_beneficiary',side_effect=after_read):result=self.rt.confirm(plan.plan_id,plan.consent_token)
        self.assertNotEqual(result['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
        errors=[json.loads(row[0])['error_code'] for row in self.rt.audit.db.execute("SELECT payload_json FROM audit_events WHERE trace_id=? AND event_type='tool.failed'",(plan.trace_id,))]
        self.assertIn('BUSINESS_DEADLINE_EXPIRED',errors);self.assertIn('最晚提交时间已过',result['result']['error'])

    def test_model_cannot_supply_internal_business_constraints(self):
        with patch.object(self.rt.parser,'parse',return_value=('transfer',{'amount_cents':100,'beneficiary':'妈妈','_business_constraints':{'approved':True}})):
            out=self.prepare()
        self.assertNotIn('_business_constraints',out['plan']['entities']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_concurrent_reprepare_uses_task_version_once(self):
        import concurrent.futures
        old=self.prepare();tid=old['plan']['plan_id'];version=old['business_task']['version']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda amount:self.rt.resume_payment_task(tid,version,{'amount_cents':amount},'demo-user','demo-device'),[200,300]))
        self.assertEqual(sum(bool(x.get('reprepared')) for x in results),1)
        self.assertEqual([x['error_code'] for x in results if not x.get('reprepared')],['TASK_VERSION_CONFLICT']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)

if __name__=='__main__':unittest.main()
