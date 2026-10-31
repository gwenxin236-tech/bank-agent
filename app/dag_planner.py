"""P06 structured DAG proposals. No tools, banking imports or authority here."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timezone
import hashlib,json,re
from task_dag import DagNode,TaskDag,_instant

MAX_NODES=32;MAX_EDGES=128;MAX_DEPTH=16;MAX_WAIT_SECONDS=3600
EXTRA_READS={'get_balance':'核对本人可用余额','list_transactions':'核对本人交易记录','analyze_bills':'核对本人账单'}


class DagProposalError(ValueError):pass


def topology_digest(dag):
    value={'dag_id':dag.dag_id,'plan_id':dag.plan_id,'trace_id':dag.trace_id,'actor_id':dag.actor_id,
        'device_id':dag.metadata.get('device_id'),
        'nodes':[{k:getattr(n,k) for k in ('node_id','node_type','tool','params','depends_on','compensation')} for n in dag.nodes]}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def catalogue(plan,registry,allowed_reads):
    return {'schema':1,'steps':[{'id':s.id,'tool':s.tool,'side_effect':registry[s.tool]['side_effect']} for s in plan.steps],
        'extra_read_tools':sorted(set(allowed_reads)&set(EXTRA_READS)),
        'constraints':{'max_nodes':MAX_NODES,'max_edges':MAX_EDGES,'max_depth':MAX_DEPTH,
                       'mandatory_steps':'every step_ref exactly once; all preceding original steps before each side effect',
                       'params':'no business parameters; platform binds trusted request entities'}}


def generate(plan,registry,allowed_reads,*,mode='offline',client=None):
    if mode=='model':
        if client is None:raise DagProposalError('model unavailable')
        prompt=('Return exactly JSON {"schema":1,"nodes":[...]}. Every node is a flat object. '
                'The allowed shapes are: '
                '{"id":"n1","kind":"action","step_ref":"s1","depends_on":[]}; '
                '{"id":"balance","kind":"action","tool":"get_balance","depends_on":[]}; '
                '{"id":"gate","kind":"human_gate","depends_on":["n1"]}; '
                '{"id":"wait","kind":"delay","run_at":"ISO8601 with timezone","depends_on":["n1"]}. '
                'These are schema examples, not the required graph. Use actual supplied step IDs and allowed extra reads. '
                'step_ref and tool are top-level fields, never inside an action object. '
                'human_gate has only id, kind, depends_on. No extra fields anywhere. '
                'Include every supplied step_ref exactly once. Before a side effect all preceding mandatory '
                'steps must be ancestors. No actor, money, business arguments, authority or result. '
                'Avoid cycles, obey supplied budgets. Platform adds mandatory confirmation; no extra gate is required.')
        result,reason=client.complete_json(prompt,json.dumps(catalogue(plan,registry,allowed_reads),sort_keys=True))
        if reason!='accepted' or not isinstance(result,dict):raise DagProposalError('model proposal unavailable: '+str(reason))
        return result,'model_untrusted'
    if mode!='offline':raise DagProposalError('unknown planner mode')
    nodes=[]
    if plan.intent=='transfer':
        reads=[s for s in plan.steps if not registry[s.tool]['side_effect']]
        nodes=[{'id':s.id,'kind':'action','step_ref':s.id,'depends_on':[]} for s in reads]
        join=[s.id for s in reads]
        if 'get_balance' in allowed_reads and not any(s.tool=='get_balance' for s in plan.steps):
            name='balance_check'
            while any(s.id==name for s in plan.steps):name+='x'
            nodes.append({'id':name,'kind':'action','tool':'get_balance','depends_on':[]});join.append(name)
        for s in plan.steps:
            if registry[s.tool]['side_effect']:
                nodes.append({'id':s.id,'kind':'action','step_ref':s.id,'depends_on':join[:]});join=[s.id]
    else:
        prior=[]
        for s in plan.steps:
            nodes.append({'id':s.id,'kind':'action','step_ref':s.id,'depends_on':prior[:]});prior=[s.id]
    return {'schema':1,'nodes':nodes},'offline_structured'


@dataclass
class ReviewedDag:
    dag:TaskDag
    steps:tuple
    budget:dict


def review(plan,proposal,registry,allowed_reads,*,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        raw=json.dumps(proposal,allow_nan=False,ensure_ascii=False)
        if len(raw.encode())>16384:raise ValueError('proposal too large')
        if not isinstance(proposal,dict) or set(proposal)!={'schema','nodes'} or type(proposal['schema']) is not int or proposal['schema']!=1:raise ValueError('invalid proposal schema')
        items=proposal['nodes']
        if not isinstance(items,list) or not 1<=len(items)<MAX_NODES:raise ValueError('node budget exceeded')
        originals={s.id:s for s in plan.steps}
        if len(originals)!=len(plan.steps) or any(s.tool not in registry or s.status!='pending' for s in plan.steps):raise ValueError('source plan not pristine')
        allowed_reads=set(allowed_reads)&set(EXTRA_READS)
        base={k:deepcopy(v) for k,v in plan.entities.items() if v is not None and k!='_dynamic_dag_sha256'}
        nodes=[];steps=[];refs={};extra=0;wait=0
        for row in items:
            if not isinstance(row,dict):raise ValueError('node must be an object')
            name=row.get('id');kind=row.get('kind');deps=row.get('depends_on')
            if (not isinstance(name,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',name)
                or name.startswith(('platform-','resume-confirmation'))):raise ValueError('invalid/reserved node id')
            if not isinstance(deps,list) or any(not isinstance(d,str) for d in deps):raise ValueError('invalid dependency list')
            common={'id','kind','depends_on'}
            if kind=='action':
                if set(row)==common|{'step_ref'}:
                    ref=row['step_ref']
                    if not isinstance(ref,str) or ref not in originals or ref in refs:raise ValueError('unknown/repeated mandatory step')
                    step=originals[ref];refs[ref]=name;tool=step.tool;title=step.title
                elif set(row)==common|{'tool'}:
                    tool=row['tool'];extra+=1
                    if not isinstance(tool,str) or tool not in allowed_reads or registry[tool]['side_effect'] or extra>12:raise ValueError('extra tool not authorized read')
                    title=EXTRA_READS[tool]
                else:raise ValueError('action parameters/authority/results forbidden')
                nodes.append(DagNode(name,tool=tool,params=deepcopy(base),depends_on=deps[:]))
                steps.append((name,title,tool))
            elif kind=='human_gate' and set(row)==common:
                nodes.append(DagNode(name,node_type=kind,depends_on=deps[:]))
            elif kind=='delay' and set(row)==common|{'run_at'}:
                instant=_instant(row['run_at']);delta=(instant-now).total_seconds()
                if not -60<=delta<=MAX_WAIT_SECONDS:raise ValueError('delay outside bounded horizon')
                wait+=max(0,delta)
                nodes.append(DagNode(name,node_type=kind,params={'run_at':row['run_at']},depends_on=deps[:]))
            else:raise ValueError('unknown node kind or fields')
        if set(refs)!=set(originals):raise ValueError('mandatory steps missing')
        if wait>MAX_WAIT_SECONDS:raise ValueError('total wait budget exceeded')
        # Validate the input before adding trusted confirmation dependencies.
        rawdag=TaskDag('plan-'+plan.plan_id,plan.plan_id,plan.intent,plan.trace_id,plan.actor_id,nodes,
                       metadata={'device_id':plan.device_id})
        ancestors={};depth={}
        for n in rawdag.topological_order():
            ancestors[n.node_id]=set(n.depends_on)
            for dep in n.depends_on:ancestors[n.node_id].update(ancestors[dep])
            depth[n.node_id]=1+max((depth[d] for d in n.depends_on),default=0)
        prior=[]
        for s in plan.steps:
            if registry[s.tool]['side_effect'] and not set(prior)<=ancestors[refs[s.id]]:
                raise ValueError('side effect lacks required source-plan predecessors')
            prior.append(refs[s.id])
        gate=DagNode('platform-confirmation',node_type='human_gate',params={'requires_confirmation':True,'requires_strong_auth':bool(plan.requires_strong_auth)})
        for n in nodes:
            if not n.depends_on:n.depends_on=[gate.node_id]
        nodes.insert(0,gate)
        edges=sum(len(n.depends_on) for n in nodes)
        if edges>MAX_EDGES or max(depth.values())+1>MAX_DEPTH:raise ValueError('edge/depth budget exceeded')
        dag=TaskDag(rawdag.dag_id,plan.plan_id,plan.intent,plan.trace_id,plan.actor_id,nodes,
            metadata={'device_id':plan.device_id,'source':'dynamic_plan','execution_owner':'dag'})
        return ReviewedDag(dag,tuple(steps),{'nodes':len(nodes),'edges':edges,'depth':max(depth.values())+1,'wait_seconds':int(wait),'extra_reads':extra})
    except (ValueError,TypeError,KeyError,RecursionError) as exc:
        raise DagProposalError(str(exc)) from exc
