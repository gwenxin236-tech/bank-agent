import unittest,tempfile,json
from pathlib import Path
from prototype_user_study import validate_study
class PrototypeStudyTests(unittest.TestCase):
 def test_empty_is_pending_not_human_pass(self):
  result=validate_study({'schema':1,'provenance':'real_people','consent':False,'samples':[]});self.assertEqual(result['status'],'PENDING');self.assertEqual(result['count'],0);self.assertFalse(result['identity_verified'])
 def sample(self):return {'participant_id':'p_abcd','reviewers':['p_efgh','p_ijkl'],'submitted_at':'2026-10-31T12:00:00+00:00','modality':'text','scenario':'prototype_b','question':'这笔交易不是我做的怎么办','expected_outcome':'needs_review','observed_outcome':'completed'}
 def test_wrong_human_outcome_is_retained_as_failure(self):
  result=validate_study({'schema':1,'provenance':'real_people','consent':True,'samples':[self.sample()]});self.assertEqual(result['status'],'READY_ATTESTED');self.assertEqual(result['correct'],0);self.assertEqual(len(result['failures']),1);self.assertFalse(result['identity_verified'])
 def test_private_unconsented_and_self_review_are_rejected(self):
  for mode in ['secret','consent','self_review']:
   row=self.sample();data={'schema':1,'provenance':'real_people','consent':True,'samples':[row]}
   if mode=='secret':row['question']='我的验证码是123456'
   elif mode=='consent':data['consent']=False
   else:row['reviewers']=['p_abcd','p_efgh']
   with self.assertRaises(ValueError):validate_study(data)
if __name__=='__main__':unittest.main()
