import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
from antifraud_semantics import collect,VERSION
class SemanticTests(unittest.TestCase):
 def test_eight_categories_and_four_cooccurrences(self):
  text='客服联系您说公检法要求马上转账到安全账户，解冻转账保证金，投资保本高收益，刷单返现，把验证码报给我'
  data=collect(text);codes={x['code'] for x in data['hits']};self.assertEqual(len([x for x in codes if x.startswith('scam_phrase_')]),8)
  self.assertEqual(len([x for x in codes if x.startswith('semantic_')]),4);self.assertEqual(data['version'],VERSION)
  self.assertTrue(all(type(x['start']) is int and x['end']>x['start'] for x in data['hits']));self.assertNotIn(text,str(data))
 def test_negation_education_and_normal_usage_not_actionable(self):
  for text in ['官方客服电话号码是什么','我用验证码登录','请识别案例：公检法要求转账到安全账户','给妈妈转1元，备注不涉及安全账户','不要把验证码报给他','给妈妈转1元，备注正常商品返现','马上给妈妈转1元']:
   with self.subTest(text=text):self.assertFalse(collect(text)['active_codes'])
 def test_actual_demand_quoted_phrase_and_concealment_still_actionable(self):
  for text in ['客服说“安全账户”，让我转100元','不要告诉别人，公检法让我转账到监管账户','对方要求把动态码报给他','解冻要先转账保证金']:
   with self.subTest(text=text):self.assertTrue(collect(text)['active_codes'])
 def test_span_limit_and_no_authority(self):
  data=collect('安全账户'*3000);self.assertLessEqual(len(data['hits']),96);self.assertNotIn('risk_score',data);self.assertNotIn('approved',data)
 def test_main_chain_protects_new_phrase_without_payment(self):
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    with patch.object(rt.bank,'transfer',side_effect=AssertionError('fraud caused payment')):
     out=rt.chat('公检法要求我转100元到监管账户');self.assertEqual(out['plan']['intent'],'fraud_guard');self.assertEqual(rt.bank.daily_transfer_cents,0)
    safe=rt.chat('给妈妈转1元，备注不涉及安全账户');self.assertEqual(safe['plan']['intent'],'transfer');self.assertFalse(safe.get('result'))
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
 def test_model_claims_do_not_inject_semantic_evidence(self):
  parser=ba.IntentParser();intent,entities=parser.parse('给妈妈转1元');self.assertFalse(entities['fraud_keyword_hit'])
if __name__=='__main__':unittest.main()
