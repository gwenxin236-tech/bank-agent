import sys,os,tempfile,unittest,copy,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
import banking_agent as ba
from unittest.mock import patch
from datetime import datetime,timezone,timedelta


class DynamicDagTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'dag.sqlite3',offline=True)
        self.response=self.rt.chat('给妈妈转1元');self.plan=self.response['plan'];self.token=self.response['plan_view']['action_token']
    def tearDown(self):
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
    def propose(self,proposal=None):return self.rt.dynamic_dag('propose',self.plan['plan_id'],'demo-user','demo-device',proposal=proposal)
    def adopt(self,proposal):return self.rt.dynamic_dag('adopt',self.plan['plan_id'],'demo-user','demo-device',proposal=proposal,action_token=self.token)
    def test_branch_join_adopts_without_debit_and_original_confirmation_executes_once(self):
        before=self.rt.bank.balance().data['available_cents'];p=self.propose();self.assertTrue(p['ok'])
        out=self.adopt(p['proposal']);self.assertTrue(out['ok']);self.assertNotEqual(out['plan_view']['action_token'],self.token)
        self.assertEqual(self.rt.bank.balance().data['available_cents'],before)
        self.assertFalse(self.rt.confirm(self.plan['plan_id'],self.token)['ok'])
        graph=self.rt.dag_store.get(out['dag_id']);self.assertTrue(any(len(n.depends_on)>=2 for n in graph.nodes))
        done=self.rt.confirm(self.plan['plan_id'],out['plan_view']['action_token'])
        self.assertEqual(done['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.balance().data['available_cents'],before-100)
        self.assertFalse(self.rt.confirm(self.plan['plan_id'],out['plan_view']['action_token'])['ok'])
        self.assertTrue(self.rt.audit.verify_trace(self.plan['trace_id'])['valid'])
    def test_invalid_graphs_and_foreign_identity_never_change_old_card(self):
        p=self.propose();self.assertTrue(p['ok'])
        for kind in ['cycle','missing','extra','money','repeat_write']:
            graph=copy.deepcopy(p['proposal']);nodes=graph['nodes']
            if kind=='cycle':nodes[0]['depends_on']=[nodes[-1]['id']];nodes[-1]['depends_on']=[nodes[0]['id']]
            if kind=='missing':nodes.pop()
            if kind=='extra':graph['actor_id']='other'
            if kind=='money':nodes[0]['params']={'amount_cents':1.2}
            if kind=='repeat_write':nodes.append(dict(nodes[-1],id='extra_write'))
            self.assertFalse(self.adopt(graph)['ok'],kind)
        self.assertFalse(self.rt.dynamic_dag('propose',self.plan['plan_id'],'other','demo-device')['ok'])
        self.assertEqual(self.rt.pending[self.plan['plan_id']].consent_token,self.token)

    def test_red_graph_requires_fresh_bound_strong_authentication(self):
        r=self.rt.chat('给妈妈转2000元');self.plan=r['plan'];self.token=r['plan_view']['action_token']
        challenge=self.rt.confirm(self.plan['plan_id'],self.token);old=challenge['auth_challenge']['credential_id']
        self.assertTrue(self.rt.verify(self.plan['plan_id'],self.token,'123456',old)['ok'])
        adopted=self.adopt(self.propose()['proposal']);token=adopted['plan_view']['action_token']
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
        self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'dag.sqlite3',offline=True)
        self.assertFalse(self.rt.confirm(self.plan['plan_id'],token,old)['ok'])
        new=self.rt.confirm(self.plan['plan_id'],token);self.assertTrue(new['needs_auth'])
        fresh=new['auth_challenge']['credential_id'];self.assertNotEqual(fresh,old)
        self.assertTrue(self.rt.verify(self.plan['plan_id'],token,'123456',fresh)['ok'])
        self.assertEqual(self.rt.confirm(self.plan['plan_id'],token,fresh)['result']['status'],'SUCCESS')

    def test_graph_digest_rejects_changed_params_before_effect(self):
        adopted=self.adopt(self.propose()['proposal']);before=self.rt.bank.balance_cents
        dag=self.rt.dag_store.get(adopted['dag_id']);dag.nodes[-1].params['amount_cents']=200
        self.rt.dag_store.save(dag,{'event_type':'qa.tamper'})
        done=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(done['result']['status'],'FAILED');self.assertEqual(self.rt.bank.balance_cents,before)
        self.assertFalse(self.rt.dag_control('approve',adopted['dag_id'],'demo-user','demo-device')['ok'])

    def test_manual_gate_requires_another_fresh_confirmation(self):
        p=self.propose()['proposal'];write=next(n for n in p['nodes'] if n.get('step_ref')==self.plan['steps'][-1]['id'])
        p['nodes'].append({'id':'review_gate','kind':'human_gate','depends_on':write['depends_on'][:]});write['depends_on']=['review_gate']
        adopted=self.adopt(p);self.assertTrue(adopted['ok'])
        before=self.rt.bank.balance_cents;done=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(done['result']['status'],'PROCESSING');self.assertEqual(self.rt.bank.balance_cents,before)
        refreshed=self.rt.dag_control('approve',adopted['dag_id'],'demo-user','demo-device')
        self.assertTrue(refreshed['ok']);fresh=refreshed['plan_view']['action_token']
        self.assertNotEqual(fresh,adopted['plan_view']['action_token'])
        self.assertEqual(self.rt.confirm(self.plan['plan_id'],fresh)['result']['status'],'SUCCESS')
        self.assertEqual(self.rt.bank.balance_cents,before-100)

    def test_future_delay_pauses_and_resume_does_not_restore_old_authority(self):
        p=self.propose()['proposal'];write=next(n for n in p['nodes'] if n.get('step_ref')==self.plan['steps'][-1]['id'])
        due=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        p['nodes'].append({'id':'delay_check','kind':'delay','depends_on':write['depends_on'][:],'run_at':due});write['depends_on']=['delay_check']
        adopted=self.adopt(p);before=self.rt.bank.balance_cents
        result=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(result['result']['status'],'PAUSED');self.assertEqual(self.rt.bank.balance_cents,before)
        class Future(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(tz)+timedelta(minutes=2)
        with patch('task_dag.datetime',Future):
            resumed=self.rt.dag_control('resume',adopted['dag_id'],'demo-user','demo-device')
            self.assertTrue(resumed['ok']);self.assertEqual(self.rt.bank.balance_cents,before)
            done=self.rt.confirm(self.plan['plan_id'],resumed['plan_view']['action_token'])
        self.assertEqual(done['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.balance_cents,before-100)

    def test_crash_restart_preserves_adopted_graph_and_single_effect(self):
        adopted=self.adopt(self.propose()['proposal']);before=self.rt.bank.balance_cents
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
        self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'dag.sqlite3',offline=True)
        done=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(done['result']['status'],'SUCCESS');self.assertEqual(self.rt.bank.balance_cents,before-100)

    def test_unknown_result_never_retries_and_handoff_cannot_mark_success(self):
        adopted=self.adopt(self.propose()['proposal']);self.rt.bank.fault_injection='timeout'
        with patch.object(self.rt.bank,'transfer',wraps=self.rt.bank.transfer) as transfer:
            out=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
            self.assertIn(out['result']['status'],{'PROCESSING','UNKNOWN'})
            self.rt.advance_dags()
            handoff=self.rt.dag_control('approve',adopted['dag_id'],'demo-user','demo-device',decision='handoff')
            self.assertTrue(handoff['ok']);self.assertEqual(self.rt.dag_store.get(adopted['dag_id']).status,'handoff')
            self.assertEqual(transfer.call_count,1)

    def test_storage_failure_fails_closed_without_falling_back_to_old_execution(self):
        import sqlite3
        proposal=self.propose()['proposal'];before=self.rt.bank.balance_cents
        with patch.object(self.rt.dag_store,'save',side_effect=sqlite3.OperationalError('qa unavailable')):
            result=self.adopt(proposal)
        self.assertEqual(result['error_code'],'RECOVERY_UNAVAILABLE')
        self.assertEqual(self.rt.confirm(self.plan['plan_id'],self.token)['error_code'],'RECOVERY_UNAVAILABLE')
        self.assertEqual(self.rt.bank.balance_cents,before)

    def test_model_receives_only_public_catalog_and_malicious_proposals_are_rejected(self):
        from dag_planner import generate
        class Fake:
            def complete_json(inner,system,text):
                self.assertNotIn('妈妈',text);self.assertNotIn('amount_cents',text);self.assertNotIn(self.plan['plan_id'],text)
                p=copy.deepcopy(self.propose()['proposal']);p['risk_level']='green';return p,'accepted'
        proposal,source=generate(self.rt.pending[self.plan['plan_id']],ba.TOOL_REGISTRY,[],mode='model',client=Fake())
        self.assertEqual(source,'model_untrusted');self.assertFalse(self.adopt(proposal)['ok'])

    def test_real_provider_nested_action_schema_is_rejected_without_consuming_card(self):
        before=self.rt.bank.balance_cents
        nodes=[{'id':step['id'],'kind':'action','depends_on':([] if i==0 else [self.plan['steps'][i-1]['id']]),
                'action':{'step_ref':step['id']}} for i,step in enumerate(self.plan['steps'])]
        malformed={'schema':1,'nodes':nodes}
        self.assertFalse(self.propose(malformed)['ok'])
        self.assertFalse(self.adopt(malformed)['ok'])
        self.assertEqual(self.rt.bank.balance_cents,before)
        self.assertEqual(self.rt.pending[self.plan['plan_id']].consent_token,self.token)
        self.assertTrue(self.rt.audit.verify_trace(self.plan['trace_id'])['valid'])

    def test_pause_during_tool_stops_at_persisted_boundary_then_reconfirms(self):
        import threading
        adopted=self.adopt(self.propose()['proposal']);original=self.rt._execute_step;before=self.rt.bank.balance_cents
        def execute(plan,step):
            out=original(plan,step)
            if step.tool=='resolve_beneficiary':
                t=threading.Thread(target=lambda:self.rt.dag_control('pause',adopted['dag_id'],'demo-user','demo-device'))
                t.start();t.join(timeout=3);self.assertFalse(t.is_alive())
            return out
        with patch.object(self.rt,'_execute_step',side_effect=execute):
            out=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(out['result']['status'],'PAUSED');self.assertEqual(self.rt.bank.balance_cents,before)
        resumed=self.rt.dag_control('resume',adopted['dag_id'],'demo-user','demo-device')
        self.assertEqual(self.rt.confirm(self.plan['plan_id'],resumed['plan_view']['action_token'])['result']['status'],'SUCCESS')

    def test_pause_after_completed_effect_reports_fact_without_claiming_no_debit(self):
        import threading
        adopted=self.adopt(self.propose()['proposal']);original=self.rt._execute_step;before=self.rt.bank.balance_cents
        def execute(plan,step):
            out=original(plan,step)
            if step.tool=='create_transfer':
                t=threading.Thread(target=lambda:self.rt.dag_control('pause',adopted['dag_id'],'demo-user','demo-device'))
                t.start();t.join(timeout=3)
            return out
        with patch.object(self.rt,'_execute_step',side_effect=execute):out=self.rt.confirm(self.plan['plan_id'],adopted['plan_view']['action_token'])
        self.assertEqual(self.rt.bank.balance_cents,before-100)
        self.assertNotIn('未发生扣款',out['message'])

    def test_schema_budget_and_randomized_branch_topologies(self):
        from dag_planner import review,DagProposalError
        import random
        rng=random.Random(20261024);source=self.rt.pending[self.plan['plan_id']];base=self.propose()['proposal']
        for _ in range(80):
            p=copy.deepcopy(base);write=p['nodes'][-1]
            extra=[]
            for i in range(rng.randrange(1,6)):
                name='read_'+str(i);parents=rng.sample(extra,rng.randrange(len(extra)+1))
                p['nodes'].append({'id':name,'kind':'action','tool':'get_balance','depends_on':parents});extra.append(name)
            write['depends_on']+=extra;rng.shuffle(p['nodes'])
            graph=review(source,p,ba.TOOL_REGISTRY,['get_balance'])
            self.assertEqual(sum(n.tool=='create_transfer' for n in graph.dag.nodes),1)
            bad=copy.deepcopy(p);first=next(n for n in bad['nodes'] if n.get('step_ref')==self.plan['steps'][0]['id'])
            first['depends_on']=[write['id']]
            with self.assertRaises(DagProposalError):review(source,bad,ba.TOOL_REGISTRY,['get_balance'])
        for kind in ['nodes','depth','bytes','unregistered','status','bad_delay']:
            p=copy.deepcopy(base)
            if kind=='nodes':p['nodes']*=20
            elif kind=='depth':
                previous=[]
                for i in range(16):
                    p['nodes'].append({'id':'gate_'+str(i),'kind':'human_gate','depends_on':previous});previous=['gate_'+str(i)]
                p['nodes'][0]['depends_on']+=previous
            elif kind=='bytes':p['extra']='x'*17000
            elif kind=='unregistered':p['nodes'].append({'id':'shell','kind':'action','tool':'shell_exec','depends_on':[]})
            elif kind=='status':p['nodes'][0]['status']='succeeded'
            else:p['nodes'].append({'id':'delay','kind':'delay','depends_on':[],'run_at':'2099-01-01'})
            with self.assertRaises(DagProposalError):review(source,p,ba.TOOL_REGISTRY,['get_balance'])


class DynamicBrowserTests(unittest.TestCase):
    def test_actual_browser_preview_adopt_then_explicit_confirm(self):
        import subprocess,threading
        node=os.getenv('C7_NODE_BINARY');module=os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('Node/Playwright must be configured for full browser QA')
        with tempfile.TemporaryDirectory() as tmp:
            rt=ba.AgentRuntime(db_path=Path(tmp)/'browser.sqlite3',offline=True)
            class Quiet(ba.Handler):
                def log_message(self,*args):pass
            server=ba.ThreadingHTTPServer(('127.0.0.1',0),Quiet)
            script=r'''
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const b=await chromium.launch({headless:true});try{const p=await b.newPage({viewport:{width:1440,height:1000}});const errors=[];p.on('pageerror',e=>errors.push(e.message));
await p.goto(process.env.DYNAMIC_TEST_URL);await p.locator('#messageInput').fill('给妈妈转1元');await p.locator('#sendBtn').click();await p.locator('#confirmBtn').waitFor();
const old=await p.evaluate(()=>currentPlan.action_token);await p.locator('#dynamicDagPanel summary').click();await p.locator('#dynamicDagPropose').click();
await p.locator('#dynamicDagGraph svg').waitFor();await p.locator('#dynamicDagAdopt').click();await p.waitForFunction(t=>currentPlan.action_token!==t,old);
assert.equal(await p.locator('#confirmBtn').count(),1);if(process.env.DYNAMIC_DAG_SCREENSHOT_DIR)await p.screenshot({path:process.env.DYNAMIC_DAG_SCREENSHOT_DIR+'/dynamic-preview.png',fullPage:true});
await p.locator('#confirmBtn').click();await p.waitForFunction(()=>currentResultView?.status==='SUCCESS');
assert.deepEqual(errors,[]);console.log('DYNAMIC_BROWSER_PASS');}finally{await b.close()}})().catch(e=>{console.error(e);process.exit(1)});
'''
            before=rt.bank.balance_cents
            try:
                with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True):
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    env=dict(os.environ,DYNAMIC_TEST_URL=f'http://127.0.0.1:{server.server_port}')
                    r=subprocess.run([node,'-e',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=70)
                    self.assertEqual(r.returncode,0,r.stdout.decode('utf-8',errors='replace'))
                    self.assertEqual(rt.bank.balance_cents,before-100)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()


if __name__=='__main__':unittest.main()
