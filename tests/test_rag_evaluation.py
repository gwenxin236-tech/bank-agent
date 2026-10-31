import sys,unittest,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rag_evaluation import validate_user_study,metrics
class EvaluationTests(unittest.TestCase):
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
