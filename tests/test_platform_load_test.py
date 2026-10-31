"""P10 metrics cannot turn expected rejection into successful business goodput."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from platform_load_test import summarize,validate_plan,operation_kind,guard_connect,evaluate_gates,is_timeout


class PlatformLoadTests(unittest.TestCase):
    def plan(self):return json.loads((ROOT/'config/performance_plan.json').read_text(encoding='utf-8'))
    def test_429_rejection_error_and_success_are_distinct(self):
        rows=[dict(ms=10,ok=True,status=200),dict(ms=20,ok=False,status=429),dict(ms=30,ok=False,status=403,expected_rejection=True),dict(ms=40,ok=False,status=500),dict(ms=50,ok=False,status=0,timeout=True)]
        x=summarize(rows,2)
        self.assertEqual(x['successful'],1);self.assertEqual(x['rate_limited'],1);self.assertEqual(x['expected_rejections'],1)
        self.assertEqual(x['errors'],2);self.assertEqual(x['timeouts'],1);self.assertEqual(x['goodput_rps'],.5);self.assertEqual(x['throughput_rps'],2.5)
    def test_percentiles_include_failed_latency_and_empty_is_not_zero(self):
        x=summarize([dict(ms=n,ok=True,status=200) for n in [1,2,3,4,5]],1)
        self.assertEqual(x['p50_ms'],3);self.assertAlmostEqual(x['p95_ms'],4.8)
        self.assertIsNone(summarize([],1)['p99_ms'])
    def test_unknown_is_reported_even_with_http_200(self):
        x=summarize([dict(ms=1,ok=False,status=200,unknown=True)],1);self.assertEqual(x['unknown'],1);self.assertEqual(x['errors'],1)
    def test_plan_bounds_prevent_unbounded_or_external_target(self):
        self.assertEqual(validate_plan(self.plan())['schema'],1)
        for key,value in [('soak_seconds',100000),('stage_operation_cap',100000),('concurrency',[100]),('target_url','https://bank.invalid')]:
            p=self.plan();p[key]=value
            with self.assertRaises(ValueError):validate_plan(p)
    def test_mix_is_deterministic_and_includes_complete_write_work(self):
        kinds=[operation_kind(i) for i in range(10)];self.assertEqual(kinds.count('read'),6);self.assertEqual(kinds.count('transfer'),3);self.assertEqual(kinds.count('support'),1)
    def test_network_guard_rejects_hostnames_and_public_addresses(self):
        with patch('platform_load_test._ORIGINAL_CONNECT',return_value='ok'):
            self.assertEqual(guard_connect(None,('127.0.0.1',1)),'ok')
            for address in [('1.1.1.1',443),('localhost',8090),'/tmp/socket']:
                with self.assertRaises(OSError):guard_connect(None,address)
    def test_gates_do_not_promote_missing_evidence(self):
        result=evaluate_gates({},self.plan());self.assertFalse(all(result.values()));self.assertFalse(result['complete_required_groups'])
    def test_unobserved_operation_has_no_latency_pass(self):
        report={'stages':[{'concurrency':1,'p99_ms':1,'errors':0,'unknown':0,'timeouts':0,'by_operation':{'read':{'p95_ms':1},'transfer':{'p95_ms':1},'support':{'p95_ms':None}}}]}
        self.assertFalse(evaluate_gates(report,self.plan())['single_user_latency'])
    def test_native_load_requires_owned_paths(self):
        from fineract_load_test import local_scope
        with self.assertRaises(ValueError):local_scope(ROOT/'app',ROOT/'data/verification/native.json')
        with self.assertRaises(ValueError):local_scope(ROOT/'data/verification/core',ROOT/'README.md')
    def test_wrapped_transport_timeouts_are_not_lost(self):
        from urllib.error import URLError
        self.assertTrue(is_timeout(URLError(TimeoutError('test timeout'))))
        try:
            try:raise TimeoutError('synthetic timeout')
            except TimeoutError as inner:raise RuntimeError('wrapped') from inner
        except RuntimeError as error:self.assertTrue(is_timeout(error))
        self.assertFalse(is_timeout(URLError('not a timeout')))


if __name__=='__main__':unittest.main(verbosity=2)
