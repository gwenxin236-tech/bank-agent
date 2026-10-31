"""P07 public-core boundary; network-free conformance, real core QA is separate."""
import copy,json,tempfile,unittest,sys
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
from fineract_transport import FineractAPI,FineractTransport,FineractUnavailable,cents,decimal_amount


class FineractTransportTests(unittest.TestCase):
    def test_money_is_exact_and_non_cent_or_float_values_rejected(self):
        self.assertEqual(cents(Decimal('123.45')),12345);self.assertEqual(decimal_amount(12345),'123.45')
        for value in [True,1.25,Decimal('0.001'),Decimal('NaN')]:
            with self.assertRaises(ValueError):cents(value)
        for value in [True,-1,0,1.2]:
            with self.assertRaises(ValueError):decimal_amount(value)
    def test_client_rejects_remote_urls_userinfo_queries_and_bad_credentials(self):
        for url in ['http://example.com/fineract-provider','http://127.0.0.1@evil/fineract-provider','http://127.0.0.1/fineract-provider?q=x','file:///tmp']:
            with self.assertRaises(ValueError):FineractAPI(url,'user','pass')
        client=FineractAPI('http://127.0.0.1:1234/fineract-provider','user','pass');self.assertNotIn('pass',repr(client))
    def test_transfer_mapping_binds_actual_core_ids_and_rejects_changed_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies={'POST':{'resourceId':7},'GET':{'id':7,'reversed':False,'currency':{'code':'CNY'},'transferAmount':Decimal('1.00'),'transferDate':[2026,10,24],
               'transferDescription':'bankreq_test','fromAccount':{'id':1},'toAccount':{'id':2},'fromClient':{'id':11},'toClient':{'id':12}}}
            calls=[]
            def requester(method,path,body=None,key=''):
                calls.append((method,path,body,key))
                if method=='POST' and 'command=undo' in path:replies['GET']['reversed']=True
                return copy.deepcopy(replies[method])
            transport=FineractTransport(Path(tmp)/'transport.sqlite3',requester,{'account_id':1,'client_id':11,'beneficiaries':{'benef-mom':{'account_id':2,'client_id':12}}})
            request={'schema':1,'operation':'create_transfer','request_id':'bankreq_test','actor_id':'demo-user','subject_id':'demo-user','account_ref':'acct-demo','idempotency_key':'key-test',
              'payload':{'amount_cents':100,'currency':'CNY','beneficiary_id':'benef-mom','note':''}}
            try:
                result=transport.send('create_transfer',request,5);self.assertEqual(result['payload']['amount_cents'],100);self.assertEqual(result['payload']['transaction_id'],'fineract-transfer-7')
                self.assertEqual(transport.send('create_transfer',request,5),result);self.assertEqual(sum(c[0]=='POST' for c in calls),1)
                changed=copy.deepcopy(request);changed['payload']['amount_cents']=101
                with self.assertRaises(FineractUnavailable):transport.send('create_transfer',changed,5)
                replies['GET']['transferAmount']=Decimal('2.00')
                with self.assertRaises(FineractUnavailable):transport.send('query_request',request,5)
                replies['GET']['transferAmount']=Decimal('1.00')
                undo=copy.deepcopy(request);undo.update(operation='reverse_transfer',request_id='bankreq_undo',idempotency_key='undo-key')
                undo['payload'].update(transaction_id='fineract-transfer-7',reason='explicit synthetic undo');undo['payload'].pop('note',None)
                self.assertEqual(transport.send('reverse_transfer',undo,5)['status'],'reversed')
                self.assertEqual(transport.send('reverse_transfer',undo,5)['status'],'reversed');self.assertEqual(sum(c[0]=='POST' for c in calls),2)
            finally:transport.close()
    def test_card_and_unbound_reversal_requests_never_call_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[];t=FineractTransport(Path(tmp)/'t.sqlite3',lambda *args:calls.append(args),{'account_id':1,'client_id':11,'beneficiaries':{}})
            try:
                for operation in ['report_loss','unreport_loss','query_card','reverse_transfer']:
                    with self.assertRaises(FineractUnavailable):t.send(operation,{},5)
                self.assertEqual(calls,[])
            finally:t.close()

    def test_verified_savings_rail_is_not_a_fabricated_card_or_authorization(self):
        from preflight import build_payment_preflight
        from policy_config import get_policy
        from types import SimpleNamespace
        card={'items':[],'service_scope':'FINERACT_LOCAL_SIMULATOR','payment_rail':'savings_account_transfer',
              'account_active':True,'account_owner_id':'demo-user','account_ref':'fineract-account:1','card_management_supported':False}
        class Bank:
            beneficiaries={'妈妈':'demo-ref'}
            def balance(self,*args):return SimpleNamespace(ok=True,data={'available_cents':1000000})
            def daily_transfer_total_cents(self,*args):return 0
            def cards_query(self):return SimpleNamespace(ok=True,data=card)
            def confirmed_budget_commitments(self,*args):return SimpleNamespace(ok=True,data={'items':[]})
        report=build_payment_preflight(Bank(),get_policy(),amount_cents=100,beneficiary='妈妈',requires_confirmation=True).to_dict()
        self.assertEqual(report['checks']['card_status']['status'],'satisfied');self.assertFalse(report['checks']['card_status']['evidence']['applicable'])
        self.assertEqual(report['checks']['authentication']['status'],'missing')
        card['account_owner_id']='foreign'
        self.assertEqual(build_payment_preflight(Bank(),get_policy(),amount_cents=100,beneficiary='妈妈').to_dict()['checks']['card_status']['status'],'unknown')


if __name__=='__main__':unittest.main(verbosity=2)
