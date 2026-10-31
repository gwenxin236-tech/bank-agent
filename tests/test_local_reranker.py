import json,hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from local_reranker import LocalReranker,verify_reranker

class RerankerTests(unittest.TestCase):
    def test_missing_assets_never_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=LocalReranker(tmp)
            with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
                self.assertIsNone(client.score('确认卡',['操作完成要核对']))
            self.assertEqual(client.evidence['reason'],'unavailable')
    def test_invalid_or_sensitive_input_rejected_before_load(self):
        c=LocalReranker('missing')
        with patch.object(c,'_load',side_effect=AssertionError('no load')):
            for q,docs in [('', ['资料']),('验证码123456',['资料']),('普通问题',[]),('x'*401,['资料']),('普通问题',['x']*9)]:
                self.assertIsNone(c.score(q,docs))
    def test_sigmoid_and_nonfinite_outputs(self):
        self.assertAlmostEqual(LocalReranker.probability(0),0.5)
        self.assertGreater(LocalReranker.probability(1000),0.99)
        self.assertLess(LocalReranker.probability(-1000),0.01)
        for value in [float('nan'),float('inf'),True]:
            with self.assertRaises(ValueError):LocalReranker.probability(value)
    def test_corrupt_manifest_and_traversal_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'manifest.json').write_text(json.dumps({'schema':1,'files':{'../x':{}}}))
            with self.assertRaises(ValueError):verify_reranker(p)
    def test_busy_fails_fast_without_inference(self):
        c=LocalReranker('missing');c._gate.acquire()
        try:
            self.assertIsNone(c.score('普通问题',['说明资料']))
            self.assertEqual(c.evidence['reason'],'busy')
        finally:c._gate.release()
