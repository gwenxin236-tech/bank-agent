"""D4: registration is not authorization; exercise the actual Web/Agent path."""
import ast
import copy
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import http.client
from http.server import ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
DOMAINS = ('elderly', 'antifraud', 'family', 'finance_health')
NAMES = ('elderly_plain_language', 'antifraud_safety_tip', 'family_permission_guide', 'finance_health_budget', 'finance_health_subscription_governance')


class FeatureScaffoldTests(unittest.TestCase):
    def test_required_contract_and_domain_files_exist(self):
        for relative in ('app/tool_contract.py', 'app/feature_registry.py', 'app/features/__init__.py', 'config/features.json'):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for domain in DOMAINS:
            for filename in ('__init__.py', 'tools.py', 'README.md'):
                self.assertTrue((ROOT / 'app/features' / domain / filename).is_file(), (domain, filename))

    def test_domain_sources_do_not_import_core_or_other_domains(self):
        allowed = {'tool_contract', '__future__', 'typing'}
        for domain in DOMAINS:
            domain_root = ROOT / 'app/features' / domain
            paths = list(domain_root.rglob('*.py'))
            self.assertIn(domain_root / '__init__.py', paths)
            self.assertIn(domain_root / 'tools.py', paths)
            for path in paths:
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertTrue(all(n.name.split('.')[0] in allowed for n in node.names), str(path))
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:
                            base = path.parent
                            for _ in range(node.level - 1):
                                base = base.parent
                            target = base.joinpath(*(node.module or '').split('.')).resolve()
                            self.assertTrue(target == domain_root.resolve() or domain_root.resolve() in target.parents, str(path))
                        else:
                            self.assertIn(node.module, allowed, str(path))
            readme = (ROOT / 'app/features' / domain / 'README.md').read_text(encoding='utf-8')
            for marker in (f'feature/{domain}', f'tests/test_{domain}.py', 'develop', 'PolicyEngine', '生产替换点'):
                self.assertIn(marker, readme)


class FeatureRegistryTests(unittest.TestCase):
    def setUp(self):
        self.fr = importlib.import_module('feature_registry')
        self.tc = importlib.import_module('tool_contract')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = json.loads((ROOT / 'config/features.json').read_text(encoding='utf-8'))
        self.central = {'get_balance': {'side_effect': False, 'retry': 1}}
        self.original = copy.deepcopy(self.central)
        self.registry = self.fr.load_features(self.central)

    def config_path(self, config):
        path = Path(self.tmp.name) / 'features.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return path

    def test_self_registration_preserves_existing_tools(self):
        self.assertEqual(self.central['get_balance'], self.original['get_balance'])
        self.assertEqual(set(self.registry.names), set(NAMES))
        self.assertEqual(set(self.central), {'get_balance', *NAMES})
        for name in NAMES:
            self.assertEqual(self.central[name], {'side_effect': False, 'retry': 0})
            self.assertIsInstance(self.registry.get(name), self.tc.BaseTool)
            self.assertTrue(self.registry.get(name).authz_required)
        with self.assertRaises(TypeError):
            self.tc.BaseTool()

    def test_each_domain_has_independent_configuration(self):
        self.assertEqual(set(self.config), set(DOMAINS) | {'dag'})
        self.assertIs(type(self.config['dag']['enabled']), bool)
        self.assertTrue(all(type(v) is dict for v in self.config.values()))
        changed = copy.deepcopy(self.config)
        changed['elderly']['enabled'] = False
        with patch.object(importlib, 'import_module', wraps=importlib.import_module) as load:
            registry = self.fr.load_features({}, self.config_path(changed))
        self.assertNotIn('elderly_plain_language', registry.names)
        self.assertFalse(any(c.args == ('features.elderly',) for c in load.call_args_list))
        self.assertEqual(set(registry.names), set(NAMES[1:]))
        self.assertEqual(changed['antifraud'], self.config['antifraud'])

    def test_bad_configuration_fails_closed_without_partial_registration(self):
        for invalid in ([], {'elderly': []}, dict(self.config, alien={}),
                        dict(self.config, elderly={'enabled': 'false'})):
            with self.subTest(config=invalid):
                central = copy.deepcopy(self.original)
                with self.assertRaises(ValueError):
                    self.fr.load_features(central, self.config_path(invalid))
                self.assertEqual(central, self.original)

    def test_dag_toggle_is_not_a_fifth_tool_domain(self):
        for enabled in (False, True):
            config = copy.deepcopy(self.config)
            config['dag'] = {'enabled': enabled}
            path = self.config_path(config)
            with patch.object(importlib, 'import_module', wraps=importlib.import_module) as load:
                registry = self.fr.load_features({}, path)
            self.assertEqual(set(registry.names), set(NAMES))
            self.assertFalse(any(c.args == ('features.dag',) for c in load.call_args_list))
            self.assertEqual(self.fr.load_dag_enabled(path), enabled)
        del config['dag']
        self.assertFalse(self.fr.load_dag_enabled(self.config_path(config)))
        self.assertEqual(set(self.fr.load_features({}, self.config_path(config)).names), set(NAMES))

    def test_bad_dag_options_fail_closed_without_tool_publication(self):
        for option in (None, [], {}, {'enabled': 'false'}, {'enabled': 1},
                       {'enabled': True, 'tools': ['create_transfer']}):
            with self.subTest(option=option):
                path = self.config_path(dict(self.config, dag=option))
                central = copy.deepcopy(self.original)
                with self.assertRaises(ValueError):
                    self.fr.load_features(central, path)
                self.assertEqual(central, self.original)
                with self.assertRaises(ValueError):
                    self.fr.load_dag_enabled(path)

    def test_malformed_metadata_is_rejected_without_partial_publication(self):
        module = importlib.import_module('features.elderly')
        for field, value in (('name', []), ('parameters', {'x': []}), ('authz_required', 'yes')):
            with self.subTest(field=field):
                tool = copy.copy(self.registry.get('elderly_plain_language'))
                setattr(tool, field, value)
                central = copy.deepcopy(self.original)
                with patch.object(module, 'register', side_effect=lambda r: r.register(tool)):
                    with self.assertRaises(ValueError):
                        self.fr.load_features(central)
                self.assertEqual(central, self.original)
        invalid = copy.deepcopy(self.config)
        invalid['elderly']['demo_tool'] = []
        central = copy.deepcopy(self.original)
        with self.assertRaises(ValueError):
            self.fr.load_features(central, self.config_path(invalid))
        self.assertEqual(central, self.original)

    def test_registration_collision_and_wrong_domain_are_atomic(self):
        module = importlib.import_module('features.elderly')
        tool = self.registry.get('elderly_plain_language')
        foreign = self.registry.get('family_permission_guide')
        for bad_register in (lambda r: (r.register(tool), r.register(tool)), lambda r: r.register(foreign)):
            central = copy.deepcopy(self.original)
            with patch.object(module, 'register', side_effect=bad_register):
                with self.assertRaises(ValueError):
                    self.fr.load_features(central)
            self.assertEqual(central, self.original)
        central = dict(self.original, elderly_plain_language={'sentinel': True})
        before = copy.deepcopy(central)
        with self.assertRaises(ValueError):
            self.fr.load_features(central)
        self.assertEqual(central, before)

    def test_domain_cannot_register_foreign_implementation_with_own_name(self):
        module = importlib.import_module('features.elderly')
        foreign = copy.copy(self.registry.get('family_permission_guide'))
        foreign.name = 'elderly_stolen'
        with patch.object(module, 'register', side_effect=lambda r: r.register(foreign)):
            with self.assertRaises(ValueError):
                self.fr.load_features({})

    def test_registration_failure_in_later_domain_does_not_publish_partial_tools(self):
        module = importlib.import_module('features.finance_health')
        central = copy.deepcopy(self.original)
        with patch.object(module, 'register', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                self.fr.load_features(central)
        self.assertEqual(central, self.original)

    def test_side_effect_extension_requires_separate_core_onboarding(self):
        module = importlib.import_module('features.elderly')
        tool = copy.copy(self.registry.get('elderly_plain_language'))
        tool.side_effect = True
        with patch.object(module, 'register', side_effect=lambda r: r.register(tool)):
            with self.assertRaisesRegex(ValueError, 'side.effect|副作用'):
                self.fr.load_features({})

    def test_request_parser_is_explicit_and_cannot_select_builtin_tools(self):
        self.assertIsNone(self.registry.parse_request('查询余额'))
        request = self.registry.parse_request('功能演示 elderly')
        self.assertEqual(request['feature_tool'], 'elderly_plain_language')
        self.assertEqual(request['feature_params'], self.config['elderly']['demo_params'])
        unknown = self.registry.parse_request('功能演示 get_balance')
        self.assertNotIn(unknown['feature_tool'], self.central)
        self.assertEqual(unknown['requested_tool'], 'get_balance')

    def test_context_is_immutable_and_exposes_no_bank_or_runtime(self):
        ctx = self.tc.ToolContext('trace', 'task', 'node', 'alice', 'alice', 'device', True)
        with self.assertRaises(FrozenInstanceError):
            ctx.actor_id = 'bob'
        self.assertFalse(hasattr(ctx, 'bank'))
        self.assertFalse(hasattr(ctx, 'runtime'))


class FeatureRuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.ba = importlib.import_module('banking_agent')
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = self.ba.AgentRuntime(db_path=Path(self.tmp.name) / 'd4.sqlite3', offline=True)
        self.registry = self.ba.FEATURE_REGISTRY

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    @contextmanager
    def isolated_runtime(self):
        """Each fault scenario gets a fresh real breaker/database, not a mocked gate."""
        previous = self.rt
        with tempfile.TemporaryDirectory() as directory:
            self.rt = self.ba.AgentRuntime(db_path=Path(directory) / 'isolated.sqlite3', offline=True)
            try:
                yield
            finally:
                self.rt.recovery_scheduler.stop()
                self.rt.bank.close()
                self.rt.audit.db.close()
                self.rt = previous

    def state(self):
        return copy.deepcopy((self.rt.bank.balance().data, self.rt.bank.subscriptions,
                              [dict(r) for r in self.rt.bank.db.execute('SELECT * FROM core_transactions')]))

    def complete(self, text):
        out = self.rt.chat(text)
        self.assertEqual(out['plan']['intent'], 'feature_demo')
        self.assertTrue(out['plan']['requires_confirmation'])
        return out, self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])

    def assert_event(self, trace, kind):
        events = [e for e in self.rt.audit.list_for_trace(trace) if e['event_type'] == kind]
        self.assertTrue(events, kind)
        self.assertTrue(self.rt.audit.verify_trace(trace)['valid'])
        return events


class FeatureAgentTests(FeatureRuntimeFixture):
    def test_four_offline_tools_use_policy_confirmation_and_audit(self):
        before = self.state()
        for domain, name in zip(DOMAINS, NAMES):
            with self.subTest(domain=domain), patch.object(self.rt.parser, 'parse', side_effect=AssertionError('demo must be offline')), \
                 patch.object(self.rt.policy, 'classify', wraps=self.rt.policy.classify) as policy:
                out = self.ba.AgentGateway(self.rt).handle(self.ba.UnifiedMessage(
                    channel='web', user_id='demo-user', conversation_id='d4-demo', text='功能演示 ' + domain))
                self.assertEqual(out['gateway']['trace_id'], out['trace_id'])
                self.assertTrue(out['plan']['requires_confirmation'])
                done = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
                self.assertEqual(out['plan']['risk_level'], 'yellow')
                self.assertEqual(done['result']['status'], 'SUCCESS')
                result = done['result']['results'][0]
                self.assertEqual(result['tool'], name)
                self.assertTrue(result['data']['message'])
                self.assertEqual(result['trace_id'], out['trace_id'])
                self.assertTrue(any(c.args[0] == 'feature_demo' for c in policy.call_args_list))
                self.assert_event(out['trace_id'], 'tool.called')
                self.assert_event(out['trace_id'], 'tool.result')
                self.assertNotIn(out['plan']['plan_id'], self.rt.pending)
                self.assertEqual(self.state(), before)

    def test_integer_cent_budget_and_no_tool_execution_before_consent(self):
        tool = self.registry.get('finance_health_budget')
        with patch.object(tool, 'execute', wraps=tool.execute) as execute:
            out = self.rt.chat('功能演示 finance_health {"income_cents":100001,"expense_cents":30000}')
            execute.assert_not_called()
            done = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
            execute.assert_called_once()
            ctx = execute.call_args.args[0]
            self.assertEqual((ctx.actor_id, ctx.subject_id, ctx.task_id, ctx.trace_id),
                             ('demo-user', 'demo-user', out['plan']['plan_id'], out['trace_id']))
            self.assertTrue(ctx.authorized)
        data = done['result']['results'][0]['data']
        self.assertEqual(data['remaining_cents'], 70001)
        self.assertIs(type(data['remaining_cents']), int)
        self.assertIn('700.01', data['message'])

    def test_invalid_or_forged_params_rejected_without_invoking_tool(self):
        tool = self.registry.get('finance_health_budget')
        bad = ('{"income_cents":1.2,"expense_cents":0}', '{"income_cents":true,"expense_cents":0}',
               '{"income_cents":-1,"expense_cents":0}', '{"income_cents":"100","expense_cents":0}',
               '{"authorized":true,"actor_id":"bob"}', '{"risk_level":"green"}', '{bad json', '[]')
        for params in bad:
            with self.subTest(params=params), self.isolated_runtime(), patch.object(tool, 'execute', wraps=tool.execute) as execute:
                before = self.state()
                out, done = self.complete('功能演示 finance_health ' + params)
                self.assertEqual(done['result']['status'], 'FAILED')
                execute.assert_not_called()
                self.assertEqual(self.state(), before)
                self.assert_event(out['trace_id'], 'tool.denied')
                self.assert_event(out['trace_id'], 'tool.failed')

    def test_unknown_feature_and_builtin_selection_are_denied(self):
        before = self.state()
        for requested in ('elderly_missing', 'get_balance', 'create_transfer'):
            with self.subTest(tool=requested):
                out, done = self.complete('功能演示 ' + requested)
                self.assertEqual(done['result']['status'], 'FAILED')
                self.assertEqual(self.state(), before)
                self.assertEqual(self.assert_event(out['trace_id'], 'tool.denied')[-1]['payload']['reason'], 'not_registered')

    def test_direct_execution_cannot_bypass_confirmation(self):
        out = self.rt.chat('功能演示 elderly')
        plan = self.rt.pending[out['plan']['plan_id']]
        tool = self.registry.get('elderly_plain_language')
        with patch.object(tool, 'execute', wraps=tool.execute) as execute:
            result = self.rt.execute(plan)
        self.assertEqual(result['status'], 'FAILED')
        execute.assert_not_called()
        self.assert_event(out['trace_id'], 'tool.denied')

    def test_policy_denial_and_strong_auth_upgrade_are_enforced(self):
        for decision in ({'authz_allowed': False, 'decision_reason': 'fixture denied'},
                         {'authz_allowed': True, 'requires_strong_auth': True}):
            out = self.rt.chat('功能演示 elderly')
            tool = self.registry.get('elderly_plain_language')
            with patch.object(self.rt.policy, 'classify_tool', return_value=decision), \
                 patch.object(tool, 'execute', wraps=tool.execute) as execute:
                done = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
            self.assertEqual(done['result']['status'], 'FAILED')
            execute.assert_not_called()

    def test_tool_failures_and_invalid_results_are_not_retried_or_reported_success(self):
        tool = self.registry.get('elderly_plain_language')
        for mode in ('exception', 'invalid_shape', 'float_result', 'explicit_failure'):
            with self.subTest(mode=mode), self.isolated_runtime():
                self.check_failed_tool_mode(tool, mode)

    def check_failed_tool_mode(self, tool, mode):
        before = self.state()
        out = self.rt.chat('功能演示 elderly')
        options = {'side_effect': RuntimeError('private internal error')} if mode == 'exception' else {
            'return_value': {'invalid_shape': [], 'float_result': {'ok': True, 'data': {'amount_cents': 1.2}},
                             'explicit_failure': {'ok': False, 'data': {}, 'error_code': 'DEMO_REJECTED', 'error_message': '示例拒绝'}}[mode]}
        with patch.object(tool, 'execute', **options) as execute:
            done = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
            execute.assert_called_once()
            self.assertEqual(done['result']['status'], 'FAILED')
            self.assertEqual(done['result_view']['status'], 'FAILED')
            self.assertNotIn('private internal error', json.dumps(done))
            self.assertEqual(self.state(), before)
            self.assert_event(out['trace_id'], 'tool.failed')

    def test_confirmation_binds_feature_parameters(self):
        out = self.rt.chat('功能演示 finance_health')
        plan = self.rt.pending[out['plan']['plan_id']]
        plan.entities['feature_params']['income_cents'] += 1
        before = self.state()
        rejected = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertFalse(rejected['ok'])
        self.assertEqual(self.state(), before)

    def test_configured_family_accounts_do_not_gain_unapproved_feature_access(self):
        actor = next(iter(self.rt.bank.guardianships.accounts))
        out = self.rt.chat('功能演示 family', actor_id=actor)
        self.assertFalse(out['ok'])
        self.assertEqual(out['error_code'], 'AUTHZ_DENIED')
        self.assert_event(out['trace_id'], 'authz.denied')


class FeatureWebTests(FeatureRuntimeFixture):
    def test_real_web_chat_confirmation_and_audit_for_each_domain(self):
        ba = self.ba
        class QuietHandler(ba.Handler):
            def log_message(self, *args):
                pass
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), \
             patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            with ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                worker.start()
                def request(path, body=None, session=True):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
                    try:
                        headers = {'Content-Type': 'application/json'}
                        if session:
                            headers['Authorization'] = 'Bearer demo-user-session'
                        conn.request('POST' if body is not None else 'GET', path,
                                     None if body is None else json.dumps(body), headers)
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        conn.close()
                try:
                    before = self.state()
                    self.assertEqual(request('/api/chat', {'message': '功能演示 elderly'}, False)[0], 403)
                    for domain, name in zip(DOMAINS, NAMES):
                        status, out = request('/api/chat', {'message': '功能演示 ' + domain})
                        self.assertEqual(status, 200)
                        self.assertTrue(out['plan_view']['requires_confirmation'])
                        status, done = request('/api/confirm', {'plan_id': out['plan']['plan_id'], 'action_token': out['plan']['consent_token']})
                        self.assertEqual(status, 200)
                        self.assertEqual(done['result_view']['status'], 'SUCCESS')
                        self.assertEqual(done['result']['results'][0]['tool'], name)
                        status, audit = request('/api/v1/audit/events?trace_id=' + out['trace_id'])
                        self.assertEqual(status, 200)
                        evidence = [e for e in audit['events'] if e['event_type'] == 'tool.result']
                        self.assertTrue(evidence)
                        self.assertEqual(evidence[-1]['payload']['tool_result'], done['result']['results'][0]['data'])
                    self.assertEqual(self.state(), before)
                finally:
                    server.shutdown()
                    worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
