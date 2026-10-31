import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rag_load_test import percentile,summarize,make_corpus
class LoadMetricsTests(unittest.TestCase):
    def test_percentiles_are_interpolated_not_claimed_latency(self):
        self.assertEqual(percentile([1,2,3,4,5],50),3)
        self.assertAlmostEqual(percentile([1,2,3,4,5],95),4.8)
        self.assertIsNone(percentile([],99))
    def test_errors_and_degradation_are_separate(self):
        d=summarize([{'ms':10,'ok':True,'degraded':True,'busy':True},{'ms':20,'ok':False,'degraded':False,'busy':False}],1)
        self.assertEqual(d['errors'],1);self.assertEqual(d['degraded'],1);self.assertEqual(d['rate_limited'],1)
    def test_synthetic_corpus_has_bounded_unique_documents(self):
        value=make_corpus(64)
        self.assertEqual(len(value['documents']),64)
        self.assertEqual(len({d['id'] for d in value['documents']}),64)
