# D5 network tests use fault injection, never a real key.
import io
import json
import os
import socket
import urllib.error
import http.client
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from llm_adapter import LLMConfig, LLMIntentAdapter


class LLMAdapterTests(unittest.TestCase):
    def test_environment_requires_explicit_online_choice_even_with_credentials(self):
        inherited = {'LLM_API_KEY': 'fixture-secret', 'LLM_MODEL': 'fixture-model'}
        for choice in (None, '1', '', 'unexpected'):
            with self.subTest(choice=choice), patch.dict(os.environ, inherited, clear=True):
                if choice is not None:
                    os.environ['BANK_AGENT_OFFLINE'] = choice
                adapter = LLMIntentAdapter(LLMConfig.from_env())
                with patch('llm_adapter._open_request') as network:
                    self.assertIsNone(adapter.parse('查询余额'))
                network.assert_not_called()
                self.assertTrue(adapter.config.offline)
                self.assertEqual(adapter.evidence['reason'], 'offline')

    def test_explicit_online_choice_uses_only_the_mocked_transport(self):
        with patch.dict(os.environ, {'LLM_API_KEY': 'fixture-secret', 'LLM_MODEL': 'fixture-model',
                                    'BANK_AGENT_OFFLINE': '0'}, clear=True):
            adapter = LLMIntentAdapter(LLMConfig.from_env())
        with patch('llm_adapter._open_request', return_value=model_response()) as network:
            self.assertEqual(adapter.parse('查询余额'), ('balance_query', {
                'beneficiary': None, 'amount_cents': None, 'note': '', 'subscription_id': None,
                'action': None, 'goal_name': None, 'target_cents': None, 'monthly_cents': None,
                'member': None, 'allowance_cents': None, 'participants': []}))
        network.assert_called_once()

    def test_http_entry_defaults_offline_without_importing_the_service(self):
        import ast
        module = ast.parse((ROOT / 'app/banking_agent.py').read_text(encoding='utf-8'))
        assignment = next(node for node in module.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'runtime' for target in node.targets))
        class FakeRuntime:
            def __init__(self, **kwargs): self.offline = kwargs['offline']
        for choice, expected in ((None, True), ('1', True), ('0', False), ('', True), ('invalid', True)):
            with self.subTest(choice=choice), patch.dict(os.environ, {}, clear=True):
                if choice is not None: os.environ['BANK_AGENT_OFFLINE'] = choice
                value = eval(compile(ast.Expression(assignment.value), '<runtime-entry>', 'eval'),
                             {'AgentRuntime': FakeRuntime, 'os': os})
                self.assertIs(value.offline, expected)

    def test_disabled_without_credentials(self):
        adapter = LLMIntentAdapter(LLMConfig())
        self.assertFalse(adapter.config.enabled)
        self.assertIsNone(adapter.parse("查余额"))

    def test_validation_keeps_only_safe_entities(self):
        adapter = LLMIntentAdapter(LLMConfig(api_key="x", model="m"))
        result = adapter._validate({
            "intent": "transfer",
            "entities": {"beneficiary": "妈妈", "amount_cents": 20000, "note": "午餐", "risk": "red", "tool": "create_transfer"},
        })
        self.assertEqual(result[0], "transfer")
        self.assertEqual(result[1]["amount_cents"], 20000)
        self.assertNotIn("risk", result[1])
        self.assertNotIn("tool", result[1])

    def test_invalid_intent_is_rejected(self):
        adapter = LLMIntentAdapter(LLMConfig(api_key="x", model="m"))
        self.assertIsNone(adapter._validate({"intent": "create_transfer", "entities": {}}))


def model_response(content=None):
    value = {"intent": "balance_query", "entities": {}} if content is None else content
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode())


class LLMRobustnessTests(unittest.TestCase):
    def adapter(self):
        return LLMIntentAdapter(LLMConfig(api_key="fixture-secret", model="fixture-model"))

    def test_bad_timeout_env_disables_model_without_crashing(self):
        for timeout in ('abc', '', 'nan', 'inf', '-1', '0', '9999999999999999999999'):
            with self.subTest(timeout=timeout), patch.dict(os.environ, {
                    'LLM_API_KEY': 'fixture-secret', 'LLM_MODEL': 'fixture-model',
                    'LLM_TIMEOUT_SECONDS': timeout, 'BANK_AGENT_OFFLINE': '0'}, clear=True):
                config = LLMConfig.from_env()
                self.assertFalse(config.enabled)
                adapter = LLMIntentAdapter(config)
                with patch('llm_adapter._open_request') as network:
                    self.assertIsNone(adapter.parse('查询余额'))
                network.assert_not_called()
                self.assertEqual(adapter.evidence['reason'], 'invalid_config')

    def test_aliases_and_explicit_empty_key_precedence(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'alias-secret', 'OPENAI_MODEL': 'alias-model',
                                     'OPENAI_BASE_URL': 'https://example.test/v1/', 'LLM_TIMEOUT_SECONDS': '2.5',
                                     'BANK_AGENT_OFFLINE': '0'}, clear=True):
            config = LLMConfig.from_env()
            self.assertTrue(config.enabled)
            self.assertEqual(config.timeout_seconds, 2.5)
            self.assertEqual(config.base_url, 'https://example.test/v1')
            self.assertNotIn('alias-secret', repr(config))
            with patch.dict(os.environ, {'LLM_API_KEY': ''}):
                self.assertFalse(LLMConfig.from_env().enabled)

    def test_invalid_url_falls_back_before_network(self):
        for base in ('not-a-url', 'file:///tmp/key', 'https://name:password@example.test/v1',
                     'https://example.test:abc/v1', 'https://example.test/v1?secret=x'):
            with self.subTest(base=base):
                adapter = LLMIntentAdapter(LLMConfig(api_key='x', model='m', base_url=base))
                with patch('llm_adapter._open_request') as network:
                    self.assertIsNone(adapter.parse('查询余额'))
                network.assert_not_called()
                self.assertEqual(adapter.evidence['reason'], 'invalid_config')

    def test_network_failures_have_safe_reason_and_no_retry(self):
        cases = [(TimeoutError('fixture-secret'), 'timeout'),
                 (urllib.error.URLError(socket.timeout('fixture-secret')), 'timeout'),
                 (urllib.error.URLError('fixture-secret'), 'network_error'),
                 (ConnectionResetError('fixture-secret'), 'network_error'),
                 (http.client.IncompleteRead(b'fixture-secret'), 'network_error'),
                 (urllib.error.HTTPError('https://example.test', 401, 'fixture-secret', {}, None), 'http_error')]
        for error, reason in cases:
            with self.subTest(reason=reason, error=type(error).__name__):
                adapter = self.adapter()
                with patch('llm_adapter._open_request', side_effect=error) as network:
                    self.assertIsNone(adapter.parse('查询余额'))
                network.assert_called_once()
                self.assertEqual(adapter.evidence['reason'], reason)
                self.assertTrue(adapter.evidence['attempted'])
                self.assertFalse(adapter.evidence['accepted'])
                self.assertNotIn('fixture-secret', json.dumps(adapter.evidence))

    def test_bad_json_envelope_schema_and_large_response_fall_back(self):
        fixtures = [b'not JSON', b'\xff', b'{}', b'{"choices":[]}',
                    b'{"choices":[{"message":{"content":"not JSON"}}]}',
                    b'x' * 65537]
        fixtures += [model_response(v).getvalue() for v in (
            [], {'intent': [], 'entities': {}}, {'intent': 'create_transfer', 'entities': {}},
            {'intent': 'balance_query', 'entities': []})]
        for raw in fixtures:
            with self.subTest(raw=raw[:70]):
                adapter = self.adapter()
                with patch('llm_adapter._open_request', return_value=io.BytesIO(raw)) as network:
                    self.assertIsNone(adapter.parse('查询余额'))
                network.assert_called_once()
                self.assertEqual(adapter.evidence['source'], 'rules')
                self.assertFalse(adapter.evidence['accepted'])

    def test_accepted_model_output_has_truthful_evidence_and_integer_money(self):
        adapter = self.adapter()
        value = {'intent': 'transfer', 'entities': {'amount_cents': 500001, 'beneficiary': '妈妈',
                 'authorized': True, 'source': 'llm', 'tool': 'create_transfer'}}
        with patch('llm_adapter._open_request', return_value=model_response(value)) as network:
            intent, entities = adapter.parse('仅测试结构化抽取')
        network.assert_called_once()
        self.assertEqual(intent, 'transfer')
        self.assertEqual(entities['amount_cents'], 500001)
        self.assertIs(type(entities['amount_cents']), int)
        self.assertNotIn('authorized', entities)
        self.assertNotIn('source', entities)
        self.assertEqual({k: adapter.evidence[k] for k in ('source', 'attempted', 'accepted', 'reason')},
                         {'source': 'llm', 'attempted': True, 'accepted': True, 'reason': 'accepted'})
        for amount in (True, 1.2, '500001'):
            with patch('llm_adapter._open_request', return_value=model_response({'intent': 'transfer', 'entities': {'amount_cents': amount}})):
                self.assertIsNone(adapter.parse('查询'))
            self.assertEqual(adapter.evidence['reason'], 'schema_rejected')

    def test_evidence_is_request_local_not_shared_between_threads(self):
        adapter = self.adapter()
        adapter.reset_evidence('main-context')
        def parse_once():
            with patch('llm_adapter._open_request', return_value=model_response()):
                self.assertIsNotNone(adapter.parse('查询余额'))
            return adapter.evidence
        with ThreadPoolExecutor(max_workers=1) as pool:
            evidence = pool.submit(parse_once).result()
        self.assertTrue(evidence['accepted'])
        self.assertEqual(adapter.evidence['reason'], 'main-context')


class P02ModelBoundaryTests(unittest.TestCase):
    def config(self, **changes):
        return replace(LLMConfig(api_key='fixture-secret', model='fixture-model'), **changes)

    def test_siliconflow_defaults_and_explicit_offline_disable(self):
        with patch.dict(os.environ, {'LLM_PROVIDER':'siliconflow', 'LLM_API_KEY':'fixture-secret',
                                    'LLM_MODEL':'Qwen/Qwen3.5-35B-A3B', 'BANK_AGENT_OFFLINE':'1'}, clear=True):
            config = LLMConfig.from_env()
        self.assertEqual(config.base_url, 'https://api.siliconflow.cn/v1')
        adapter = LLMIntentAdapter(config)
        with patch('llm_adapter._open_request') as network:
            self.assertIsNone(adapter.parse('查询余额'))
        network.assert_not_called()
        self.assertEqual(adapter.evidence['reason'], 'offline')

    def test_credentials_never_use_plain_http_or_wrong_siliconflow_host(self):
        for base, provider in [('http://example.test/v1',''),
                               ('https://example.test/v1','siliconflow'),
                               ('https://api.siliconflow.cn/v1/chat/completions','siliconflow')]:
            with self.subTest(base=base):
                self.assertFalse(self.config(base_url=base, provider=provider).enabled)

    def test_invalid_budget_settings_disable_before_network(self):
        for setting in ('LLM_MAX_OUTPUT_TOKENS','LLM_CALLS_PER_MINUTE','LLM_MAX_CONCURRENT'):
            for bad in ('abc','0','-1','999999999'):
                with self.subTest(setting=setting,bad=bad), patch.dict(os.environ,
                        {'LLM_API_KEY':'fixture-secret','LLM_MODEL':'fixture-model',setting:bad}, clear=True):
                    self.assertFalse(LLMConfig.from_env().enabled)

    def test_private_input_is_not_sent_or_copied_to_evidence(self):
        samples=['我的手机号是13812345678，查余额', '卡号6222021234567890123',
                 '我的API密钥是sk-'+'x'*30, '验证码为123456，请查看余额']
        for text in samples:
            with self.subTest(kind=text[:4]):
                adapter=LLMIntentAdapter(self.config())
                with patch('llm_adapter._open_request') as network:
                    self.assertIsNone(adapter.parse(text))
                network.assert_not_called()
                self.assertEqual(adapter.evidence['reason'],'privacy_blocked')
                self.assertFalse(adapter.evidence['attempted'])
                self.assertNotIn(text,json.dumps(adapter.evidence))

    def test_text_bounds_fail_before_call_instead_of_silent_truncation(self):
        for text in ('','x'*2001,None):
            adapter=LLMIntentAdapter(self.config())
            with patch('llm_adapter._open_request') as network:self.assertIsNone(adapter.parse(text))
            network.assert_not_called()
            self.assertEqual(adapter.evidence['reason'],'invalid_input')

    def test_output_cap_usage_and_safe_timing_are_recorded(self):
        adapter=LLMIntentAdapter(self.config(max_output_tokens=256))
        raw=json.loads(model_response().getvalue())
        raw['usage']={'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}
        raw['choices'][0]['finish_reason']='stop'
        with patch('llm_adapter._open_request',return_value=io.BytesIO(json.dumps(raw).encode())) as network:
            self.assertIsNotNone(adapter.parse('查询余额'))
        payload=json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['max_tokens'],256)
        self.assertFalse(payload['stream'])
        self.assertEqual(adapter.evidence['usage']['total_tokens'],130)
        self.assertIs(type(adapter.evidence['elapsed_ms']),int)
        self.assertNotIn('fixture-secret',json.dumps(adapter.evidence))

    def test_duplicate_keys_and_truncated_or_tool_call_outputs_rejected(self):
        duplicate=b'{"choices":[{"message":{"content":"{\\"intent\\":\\"balance_query\\",\\"intent\\":\\"transfer\\",\\"entities\\":{}}"}}]}'
        fixtures=[duplicate]
        for finish in ('length','tool_calls','content_filter'):
            raw=json.loads(model_response().getvalue());raw['choices'][0]['finish_reason']=finish
            fixtures.append(json.dumps(raw).encode())
        raw=json.loads(model_response().getvalue());raw['choices'][0]['message']['tool_calls']=[{'id':'not-a-tool'}]
        fixtures.append(json.dumps(raw).encode())
        for raw in fixtures:
            with self.subTest(size=len(raw)), patch('llm_adapter._open_request',return_value=io.BytesIO(raw)):
                adapter=LLMIntentAdapter(self.config());self.assertIsNone(adapter.parse('查询余额'))
                self.assertFalse(adapter.evidence['accepted'])

    def test_known_fields_are_typed_and_do_not_stringify_nested_objects(self):
        adapter=LLMIntentAdapter(self.config())
        for entities in ({'beneficiary':{'name':'妈妈'}}, {'note':['hi']}, {'participants':[True]},
                         {'amount_cents':True}, {'action':'x'*33}):
            with self.subTest(field=next(iter(entities))):
                self.assertIsNone(adapter._validate({'intent':'transfer','entities':entities}))

    def test_rate_cap_counts_errors_and_does_not_retry(self):
        adapter=LLMIntentAdapter(self.config(calls_per_minute=1))
        with patch('llm_adapter._open_request',side_effect=TimeoutError('fixture-secret')) as network:
            self.assertIsNone(adapter.parse('查询余额'))
            self.assertIsNone(adapter.parse('查询余额'))
        network.assert_called_once()
        self.assertEqual(adapter.evidence['reason'],'rate_limited')
        self.assertFalse(adapter.evidence['attempted'])

    def test_concurrency_cap_is_released_even_after_failure(self):
        adapter=LLMIntentAdapter(self.config(max_concurrent=1));entered=threading.Event();release=threading.Event()
        def blocking(*args,**kwargs):
            entered.set();release.wait(5);raise TimeoutError('fixture-secret')
        with patch('llm_adapter._open_request',side_effect=blocking) as network, ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(adapter.parse,'查询余额');self.assertTrue(entered.wait(3))
            self.assertIsNone(adapter.parse('查询余额'))
            self.assertEqual(adapter.evidence['reason'],'concurrency_limit')
            release.set();future.result(timeout=5)
        with patch('llm_adapter._open_request',return_value=model_response()):self.assertIsNotNone(adapter.parse('查询余额'))
        self.assertEqual(network.call_count,1)

    def test_actual_redirect_never_forwards_credentials(self):
        class Handler(BaseHTTPRequestHandler):
            forwarded=0
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                self.send_response(302);self.send_header('Location','/stolen');self.end_headers()
            def do_GET(self):
                type(self).forwarded+=1;self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                adapter=LLMIntentAdapter(self.config(base_url=f'http://127.0.0.1:{server.server_port}/v1'))
                self.assertIsNone(adapter.parse('查询余额'))
                self.assertEqual(adapter.evidence['error_kind'],'redirect_blocked')
                self.assertEqual(Handler.forwarded,0)
            finally:server.shutdown();thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
