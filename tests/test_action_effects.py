from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
import banking_agent as ba

class ActionEffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'bank.sqlite3';self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
    def tearDown(self):
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
    def plan(self):
        out=self.rt.chat('给妈妈转1元');return out,self.rt.pending[out['plan']['plan_id']]
    def test_server_effects_describe_single_payment_without_authority(self):
        out,p=self.plan();c=p.entities['_effect_contract']
        self.assertEqual(c['scope'],'single_transfer');self.assertEqual(c['stage'],'prepare')
        self.assertEqual(c['amount_cents'],100);self.assertEqual(c['expires_at'],p.token_expires_at)
        self.assertEqual(c['tools'][0]['tool'],'create_transfer');self.assertEqual(c['tools'][0]['retry'],'never')
        self.assertNotIn('approved',c);self.assertNotIn('authorized',c)
        self.assertTrue(out['plan_view']['effect_summary'])
        self.assertNotEqual(c['target_account_sha256'],self.rt.bank.beneficiaries['妈妈'])
    def test_scope_tampering_fails_even_if_mutable_digest_is_recomputed(self):
        _,p=self.plan();p.entities['_effect_contract']['scope']='shared_default';p.entity_digest=self.rt._auth_digest(p)
        self.assertFalse(self.rt.confirm(p.plan_id,p.consent_token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_changed_amount_and_contract_cannot_reuse_original_confirmation(self):
        _,p=self.plan();p.entities['amount_cents']=200;p.entities['_effect_contract']['amount_cents']=200;p.entity_digest=self.rt._auth_digest(p)
        self.assertFalse(self.rt.confirm(p.plan_id,p.consent_token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_directory_change_requires_fresh_target_review(self):
        _,p=self.plan();self.rt.bank.beneficiaries['妈妈']='13900139000'
        self.assertFalse(self.rt.confirm(p.plan_id,p.consent_token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_target_is_rechecked_after_read_steps_inside_effect_lock(self):
        _,p=self.plan();original=self.rt.bank.resolve_beneficiary
        def changed(name):
            result=original(name);self.rt.bank.beneficiaries[name]='13900139000';return result
        with patch.object(self.rt.bank,'resolve_beneficiary',side_effect=changed):done=self.rt.confirm(p.plan_id,p.consent_token)
        self.assertEqual(done['result']['status'],'FAILED');self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_json_approval_flag_never_grants_execution(self):
        _,p=self.plan();p.entities['_effect_contract']['approved']=True;p.entity_digest=self.rt._auth_digest(p)
        self.assertFalse(self.rt.confirm(p.plan_id,p.consent_token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_legacy_plan_without_effects_requires_new_review(self):
        _,p=self.plan();p.entities.pop('_effect_contract',None);p.entity_digest=self.rt._auth_digest(p);self.rt.save_task_checkpoint(p,'waiting_confirmation')
        rejected=self.rt.confirm(p.plan_id,p.consent_token)
        self.assertFalse(rejected['ok']);self.assertEqual(rejected['error_code'],'EFFECT_REVIEW_REQUIRED')
        self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_valid_effects_work_with_existing_strong_auth_after_restart(self):
        out=self.rt.chat('给妈妈转2000元');pid=out['plan']['plan_id'];token=out['plan_view']['action_token']
        self.assertTrue(self.rt.confirm(pid,token)['needs_auth']);self.assertTrue(self.rt.verify(pid,token,'123456')['ok'])
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
        self.assertEqual(self.rt.confirm(pid,token)['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.daily_transfer_cents,200000)

    def test_constraints_reject_float_boolean_unsupported_scope_and_naive_time(self):
        from action_effects import normalize_constraints
        for value in ({'reserve_cents':1.5},{'reserve_cents':True},{'reserve_cents':-1},{'scope':'recurring'},{'deadline':'2026-11-01'}):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_constraints(value)
        self.assertEqual(normalize_constraints({'reserve_cents':100}),{'reserve_cents':100})


    def test_confirmation_expiry_cannot_be_extended_with_old_nonce(self):
        from datetime import datetime,timedelta,timezone
        _,p=self.plan();p.token_expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        p.entities['_effect_contract']['expires_at']=p.token_expires_at;p.entity_digest=self.rt._auth_digest(p)
        self.assertFalse(self.rt.confirm(p.plan_id,p.consent_token)['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)


if __name__=='__main__':unittest.main()
