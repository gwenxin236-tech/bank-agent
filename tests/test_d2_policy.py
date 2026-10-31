"""D2 single-source policy: fail closed and verify behavior in a fresh process."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


class PolicyConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'config/policy.json').read_text(encoding='utf-8'))
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'policy.json'

    def tearDown(self):
        self.tmp.cleanup()

    def run_python(self, code, config=None):
        self.path.write_text(json.dumps(self.config if config is None else config), encoding='utf-8')
        env = dict(os.environ, BANK_AGENT_POLICY_PATH=str(self.path), PYTHONPATH=str(ROOT / 'app'),
                   BANK_AGENT_DB_PATH=str(Path(self.tmp.name) / 'core.sqlite3'), PYTHONDONTWRITEBYTECODE='1',
                   PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, text=True, encoding='utf-8',
                              capture_output=True, timeout=30)

    def test_missing_invalid_and_inconsistent_config_is_rejected(self):
        from policy_config import load_policy, PolicyConfigError
        for field in self.config:
            data = copy.deepcopy(self.config)
            del data[field]
            self.path.write_text(json.dumps(data), encoding='utf-8')
            with self.subTest(missing=field), self.assertRaises(PolicyConfigError):
                load_policy(self.path)
        for value in (True, 1.5, '1000', 0, -1):
            data = dict(self.config, daily_transfer_limit_cents=value)
            self.path.write_text(json.dumps(data), encoding='utf-8')
            with self.subTest(value=value), self.assertRaises(PolicyConfigError):
                load_policy(self.path)
        data = dict(self.config, daily_transfer_limit_cents=1)
        self.path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(PolicyConfigError):
            load_policy(self.path)

    def test_default_policy_matches_existing_demo(self):
        from policy_config import load_policy
        policy = load_policy()
        self.assertEqual(policy['small_transfer_limit_cents'], 100000)
        self.assertEqual(policy['daily_transfer_limit_cents'], 10000000)
        with self.assertRaises(TypeError):
            policy['small_transfer_limit_cents'] = 1

    def test_subscription_query_requires_confirmation_even_though_tool_is_read_only(self):
        result = self.run_python('''
import banking_agent as b
r=b.AgentRuntime(offline=True)
assert 'subscription_query' not in b.POLICY['green']
assert 'subscription_query' in b.POLICY['yellow']
decision=r.policy.classify('subscription_query', {'risk_level':'green', 'requires_confirmation':False})
assert decision['risk_level']=='yellow' and decision['requires_confirmation']
assert not decision['requires_strong_auth']
for intent in ('balance_query','transaction_query','bill_analysis','investment_query','financial_health'):
    assert r.policy.classify(intent,{})['risk_level']=='green'
assert not b.TOOL_REGISTRY['list_subscriptions']['side_effect']
r.recovery_scheduler.stop(); r.bank.close(); r.audit.db.close()
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_rejects_downgrading_subscription_query_to_green(self):
        from policy_config import load_policy, PolicyConfigError
        self.config['yellow'] = [x for x in self.config['yellow'] if x != 'subscription_query']
        if 'subscription_query' not in self.config['green']:
            self.config['green'].append('subscription_query')
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with self.assertRaises(PolicyConfigError):
            load_policy(self.path)

    def test_changed_amount_limits_affect_policy_and_atomic_core(self):
        self.config.update(small_transfer_limit_cents=20000, daily_transfer_limit_cents=30000,
                           family_small_transfer_max_cents=20000)
        result = self.run_python('''
import banking_agent as b
r=b.AgentRuntime(offline=True)
assert r.chat('给妈妈转 200 元')['plan']['risk_level']=='yellow'
assert r.chat('给妈妈转 201 元')['plan']['risk_level']=='red'
assert r.bank.check_transfer_constraints(30001).error_code=='DAILY_LIMIT'
assert r.bank.transfer(30001,'妈妈','','policy:over').data['status']=='failed'
assert r.bank.balance().data['available_cents']==26853000
assert r.bank.transfer(20000,'妈妈','','policy:first').ok
assert r.bank.check_transfer_constraints(10001).error_code=='DAILY_LIMIT'
assert r.bank.guardianships.small_max==20000
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_risk_score_aml_and_fraud_rules_use_same_config(self):
        self.config.update(risk_amount_threshold_cents=80000, aml_lower_cents=70000,
                           risk_amount_points=65, risk_defer_score=65, aml_transfer_count=2,
                           fraud_new_recipient_threshold_cents=80000, cooling_off_hours=12)
        result = self.run_python('''
import banking_agent as b
from risk_controls import assess_transfer_risk
r=b.AgentRuntime(offline=True)
p=r.chat('给妈妈转 800 元')
assert p['plan']['risk_assessment']['defer']
assert p['plan']['risk_assessment']['risk_score']==65
assert r.bank.balance().data['available_cents']==26853000
assert r.bank.transfer(75000,'妈妈','','aml:one').ok
x=assess_transfer_risk(r.bank, {'amount_cents':75000,'beneficiary':'妈妈'},'demo-user')
assert x['aml_suspect'] and x['aml_count']==2
assert b.load_fraud_rules()['new_recipient_threshold_cents']==80000
assert b.load_fraud_rules()['cooling_off_hours']==12
assert '1000元' not in p['plan']['risk_assessment']['decision_reason']
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_auth_limits_and_manifest_follow_config(self):
        self.config.update(strong_auth_ttl_seconds=120, auth_max_attempts=2,
                           demo_otp='654321', http_rate_limit=17, http_rate_window_seconds=8)
        result = self.run_python('''
import banking_agent as b
from production_contracts import production_contract_manifest, MockStrongAuthProvider
r=b.AgentRuntime(offline=True)
p=r.chat('给妈妈转 5000 元')['plan']
x=r.verify(p['plan_id'],p['consent_token'],'123456')
assert x['error_code']=='AUTH_FAILED'
x=r.verify(p['plan_id'],p['consent_token'],'123456')
assert not x['ok'] and x['error_code']=='AUTH_LOCKED'
assert not r.breaker.is_locked()  # per-task attempt limit is not the global breaker
y=r.chat('给妈妈转 5000 元')['plan']
assert r.verify(y['plan_id'],y['consent_token'],'654321')['ok']
assert b.STRONG_AUTH_TTL_SECONDS==120
assert MockStrongAuthProvider.TTL_SECONDS==120
assert production_contract_manifest()['strong_auth_credential']['ttl_seconds']==120
assert b.http_rate_limiter.state()['limit']==17
assert b.http_rate_limiter.state()['window_seconds']==8
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_reports_bad_config_without_traceback_or_sensitive_value(self):
        self.config['demo_otp'] = 'secret-should-not-appear'
        result = self.run_python('''
import runpy
runpy.run_path('scripts/preflight.py',run_name='__main__')
''')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('secret-should-not-appear', result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertIn('策略配置完整性', {x['name'] for x in report['blockers']})

    def test_invalid_policy_prevents_runtime_start(self):
        del self.config['small_transfer_limit_cents']
        result = self.run_python('import banking_agent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PolicyConfigError', result.stderr)

    def test_frontend_uses_server_limit_and_demo_code(self):
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        self.assertIn('d.daily_transfer_limit_cents', js)
        self.assertNotIn('10000000-daily', js)
        self.assertNotIn('演示验证码：123456', html)
        result = self.run_python('''
import banking_agent as b
h=object.__new__(b.Handler); h.path='/api/state'
h.headers={'Authorization':'Bearer demo-user-session'}
h._send=lambda status,data:(status,data)
status,data=h.do_GET()
assert status==200 and data['daily_transfer_limit_cents']==300000
assert data['demo_otp']=='654321'
''', dict(self.config, daily_transfer_limit_cents=300000, demo_otp='654321'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_does_not_hot_reload_awaiting_tasks(self):
        result = self.run_python("""
import json, os
from policy_config import get_policy
p=get_policy()
raw=dict(p); raw['small_transfer_limit_cents']=200000
open(os.environ['BANK_AGENT_POLICY_PATH'],'w',encoding='utf-8').write(json.dumps(raw))
assert get_policy() is p and p['small_transfer_limit_cents']==100000
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_fields_and_non_readonly_green_are_rejected(self):
        from policy_config import load_policy, PolicyConfigError
        self.path.write_text('{"version":"a","version":"b"}', encoding='utf-8')
        with self.assertRaises(PolicyConfigError):
            load_policy(self.path)
        self.config['green'].append('transfer')
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with self.assertRaises(PolicyConfigError):
            load_policy(self.path)

    def test_recent_window_retry_and_idle_cash_parameters_take_effect(self):
        self.config.update(risk_recent_window_minutes=10, risk_rapid_transfer_count=2,
                           risk_rapid_points=45, retry_max_attempts=4, retry_base_delay_ms=200,
                           retry_max_delay_ms=800, idle_cash_threshold_cents=20000000)
        result = self.run_python("""
from datetime import timedelta
import banking_agent as b
from risk_controls import assess_transfer_risk
from production_contracts import retry_policy
r=b.AgentRuntime(offline=True)
assert r.bank.transfer(100,'妈妈','','recent:first').ok
x=assess_transfer_risk(r.bank, {'amount_cents':100,'beneficiary':'妈妈'}, 'demo-user')
assert x['recent_transfer_count']==1 and x['risk_score']==45
now=r.bank.risk_clock(); r.bank.risk_clock=lambda:now+timedelta(minutes=11)
x=assess_transfer_risk(r.bank, {'amount_cents':100,'beneficiary':'妈妈'}, 'demo-user')
assert x['recent_transfer_count']==0 and x['risk_score']==0
assert r.bank.idle_cash_check().data['threshold_cents']==20000000
assert retry_policy()['max_attempts']==4 and retry_policy()['base_delay_ms']==200
assert retry_policy()['max_delay_ms']==800
assert 'create_transfer' in retry_policy()['never_retry']
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_cli_accepts_explicit_file_and_never_exposes_otp(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        out = subprocess.run([sys.executable, str(ROOT / 'scripts/preflight.py'), '--policy', str(self.path)],
                             env=dict(os.environ, APP_ENV='demo', PYTHONIOENCODING='utf-8'),
                             text=True, encoding='utf-8', capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(self.config['demo_otp'], out.stdout)
        report = json.loads(out.stdout)
        self.assertTrue(next(x['ok'] for x in report['checks'] if x['name']=='策略配置完整性'))


if __name__ == '__main__':
    unittest.main()
