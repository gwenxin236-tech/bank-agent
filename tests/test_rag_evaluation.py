import sys,unittest,tempfile,json,hashlib
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rag_evaluation import validate_user_study,metrics,evaluate
class EvaluationTests(unittest.TestCase):
    def _positive_result(self, status='ANSWERED', citations=None, answer='已核对的原文'):
        root=Path(__file__).resolve().parents[1]
        raw=(root/'config/knowledge.json').read_bytes()
        doc=json.loads(raw)['documents'][0]['id']
        hit=SimpleNamespace(document_id=doc,chunk_id='fixture-chunk',text='已核对的原文')
        if citations is None:
            citations=[{'document_id':doc,'chunk_id':hit.chunk_id,'quote':hit.text}]
        class Service:
            def search(self,*args,**kwargs):return [hit]
            def answer(self,*args,**kwargs):
                self.search(*args,**kwargs)
                return {'status':status,'answer':answer,'citations':citations}
        dataset={'schema':1,'source_sha256':hashlib.sha256(raw).hexdigest(),
                 'cases':[{'id':'fixture','split':'calibration','type':'answerable',
                           'question':'确认规则','relevant_ids':[doc]}]}
        with patch('rag_evaluation.KnowledgeService.from_path',return_value=Service()), \
             patch('rag_evaluation.LocalEmbeddingClient'),patch('rag_evaluation.LocalReranker'):
            return evaluate(dataset,modes=('lexical',))['modes']['lexical']['cases'][0]

    def test_positive_retrieval_without_successful_answer_is_not_correct(self):
        row=self._positive_result(status='NO_EVIDENCE')
        self.assertTrue(row['hits'])
        self.assertFalse(row['correct'])

    def test_positive_requires_nonempty_source_checked_citation_and_answer(self):
        doc=json.loads((Path(__file__).resolve().parents[1]/'config/knowledge.json').read_text(encoding='utf-8'))['documents'][0]['id']
        for citations,answer in [([], '已核对的原文'),
                ([{'document_id':'unreviewed','chunk_id':'fixture-chunk','quote':'已核对的原文'}],'已核对的原文'),
                ([{'document_id':doc,'chunk_id':'fixture-chunk','quote':'伪造原文'}],'伪造原文'),
                ([{'document_id':doc,'chunk_id':'unknown-chunk','quote':'已核对的原文'}],'已核对的原文'),
                (None,'')]:
            with self.subTest(citations=citations,answer=answer):
                self.assertFalse(self._positive_result(citations=citations,answer=answer)['correct'])

    def test_positive_successful_answer_with_exact_citation_remains_correct(self):
        self.assertTrue(self._positive_result()['correct'])

    def test_empty_real_users_stay_pending(self):
        self.assertEqual(validate_user_study({'schema':1,'provenance':'participant_submitted','consent_local_processing':True,'samples':[]},{'a'})['status'],'PENDING')
    def test_private_query_and_missing_review_rejected(self):
        sample={'participant_id':'p_abcd','submitted_at':'2026-10-24T00:00:00+08:00','question':'验证码123456','relevant_ids':['a'],'expected_type':'answerable','reviewers':['p_efgh','p_ijkl']}
        data={'schema':1,'provenance':'participant_submitted','consent_local_processing':True,'samples':[sample]}
        with self.assertRaises(ValueError):validate_user_study(data,{'a'})
        sample['question']='确认规则';sample['reviewers']=['p_efgh']
        with self.assertRaises(ValueError):validate_user_study(data,{'a'})
    def test_unknown_source_and_false_consent_rejected(self):
        data={'schema':1,'provenance':'participant_submitted','consent_local_processing':False,'samples':[]}
        with self.assertRaises(ValueError):validate_user_study(data,{'a'})
    def test_metrics_hand_calculation_and_empty(self):
        rows=[{'expected':['a'],'hits':['x','a'],'status':'ANSWERED','correct':False},
              {'expected':[],'hits':[],'status':'NO_EVIDENCE','correct':True}]
        result=metrics(rows)
        self.assertEqual(result['hit_at_1'],0);self.assertEqual(result['hit_at_3'],1)
        self.assertEqual(result['mrr_at_3'],0.5);self.assertEqual(result['negative_refusal_rate'],1)
        self.assertIsNone(metrics([])['hit_at_1'])
