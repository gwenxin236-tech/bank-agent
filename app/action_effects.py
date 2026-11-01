"""Server-owned effects describe a proposal; only original confirmation/auth executes."""
from datetime import datetime
import hashlib,json
from domain_agent_contract import thaw

def _digest(value):return hashlib.sha256(json.dumps(thaw(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def normalize_constraints(value):
    if value is None:return {}
    if not isinstance(value,dict) or set(value)-{'deadline','reserve_cents'}:raise ValueError('Unsupported business constraints')
    out={}
    if 'reserve_cents' in value:
        reserve=value['reserve_cents']
        if type(reserve) is not int or not 0<=reserve<=2**63-1:raise ValueError('Integer reserve required')
        out['reserve_cents']=reserve
    if 'deadline' in value:
        deadline=value['deadline']
        if not isinstance(deadline,str) or len(deadline)>64:raise ValueError('Timezone-aware deadline required')
        instant=datetime.fromisoformat(deadline)
        if instant.tzinfo is None:raise ValueError('Timezone-aware deadline required')
        out['deadline']=instant.isoformat()
    return out

def build_transfer_contract(plan_id,actor_id,device_id,entities,steps,expires_at,policy,bank,registry):
    names=[getattr(s,'tool',None) for s in steps]
    writes=[name for name in names if registry.get(name,{}).get('side_effect')]
    if writes not in ([],['create_transfer']):raise ValueError('Transfer proposal exceeds one payment')
    amount=entities.get('amount_cents')
    if amount is not None and type(amount) is not int:raise ValueError('Integer cents required')
    beneficiary=entities.get('beneficiary');family=entities.get('_family') or {}
    target=bank.beneficiaries.get(beneficiary) if isinstance(beneficiary,str) else None
    constraints=normalize_constraints(entities.get('_business_constraints',{}))
    return {'schema':1,'stage':'prepare','scope':'single_transfer','task_id':plan_id,'actor_id':family.get('guardian_id',actor_id),
        'subject_id':actor_id,'device_id':device_id,'caller_device_id':family.get('guardian_device_id',device_id),
        'amount_cents':amount,'currency':'CNY','beneficiary':beneficiary,'target_account_sha256':_digest(target),
        'note':entities.get('note',''),'expires_at':expires_at,'policy_version':policy['version'],'policy_sha256':_digest(policy),
        'constraints':constraints,'tools':[{'tool':name,'side_effect':True,'scope':'single_transfer','retry':'never','reversal':'provider_specific'} for name in writes],
        'unchanged_scopes':['shared_defaults','other_products','recurring_payments'],
        'fact_recheck':['available_funds','daily_limit','beneficiary','card_status','budget'],
        'authority':'description_only'}

def validate_transfer_contract(plan,policy,bank,registry,*,recheck_target=True):
    if plan.intent!='transfer':return None
    actual=plan.entities.get('_effect_contract')
    if not isinstance(actual,dict):return 'EFFECT_REVIEW_REQUIRED'
    try:expected=build_transfer_contract(plan.plan_id,plan.actor_id,plan.device_id,plan.entities,plan.steps,plan.token_expires_at,policy,bank,registry)
    except (ValueError,TypeError,KeyError):return 'EFFECT_BINDING_MISMATCH'
    if not recheck_target:expected['target_account_sha256']=actual.get('target_account_sha256')
    if actual!=expected:return 'EFFECT_BINDING_MISMATCH'
    deadline=expected['constraints'].get('deadline')
    if recheck_target and deadline and datetime.now(datetime.fromisoformat(deadline).tzinfo)>=datetime.fromisoformat(deadline):
        return 'BUSINESS_DEADLINE_EXPIRED'
    return None

def effect_reference(contract):
    # Audit's numeric redaction must not erase a correlation digest. This is not a signature.
    return _digest(contract).translate(str.maketrans('0123456789abcdef','abcdefghijklmnop'))

def public_effect_summary(contract):
    if not isinstance(contract,dict) or contract.get('schema')!=1:return {}
    return {'scope':'仅本次转账','duration':'一次性办理，不设置周期扣款或共享默认账户',
            'reversal':'提交后的撤销、退回或冲正以实际银行支持和处理状态为准。',
            'constraints':dict(contract.get('constraints',{})),'expires_at':contract.get('expires_at',''),'notice':'这是待确认的计划说明，不代表已经授权、受理或到账。'}
