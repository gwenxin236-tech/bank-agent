"""Synthetic budget calculator. Business amounts stay integer cents."""
from tool_contract import BaseTool


def _display_cents(cents):
    whole, fraction = divmod(abs(cents), 100)
    return ('-' if cents < 0 else '') + f'{whole}.{fraction:02d}'


class BudgetTool(BaseTool):
    name = 'finance_health_budget'
    title = '整数分预算计算'
    authz_required = True
    parameters = {'income_cents': 'cents', 'expense_cents': 'cents'}

    def execute(self, ctx, **params):
        if not ctx.authorized:
            return {'ok': False, 'data': {}, 'error_code': 'AUTHZ_DENIED', 'error_message': '请先确认操作'}
        income, expense = params['income_cents'], params['expense_cents']
        if any(type(x) is not int or x < 0 for x in (income, expense)):
            return {'ok': False, 'data': {}, 'error_code': 'INVALID_AMOUNT', 'error_message': '金额必须是非负整数分'}
        remaining = income - expense
        return {'ok': True, 'data': {'income_cents': income, 'expense_cents': expense,
                                    'remaining_cents': remaining, 'demo_only': True,
                                    'message': f'按您提供的演示预算，结余 {_display_cents(remaining)} 元。仅作算术演示，不读取真实账户、不自动扣款、不承诺收益。'}}


class SubscriptionGovernanceTool(BaseTool):
    """Read-only explanation adapter for the three subscription governance layers.

    Production replacement point: the core billing/mandate/service adapters must
    provide the facts; this D4 tool never reads accounts or performs cancellation.
    """
    name = 'finance_health_subscription_governance'
    title = '订阅三层治理说明'
    authz_required = True
    parameters = {'query_text': 'text'}

    def execute(self, ctx, **params):
        if not ctx.authorized:
            return {'ok': False, 'data': {}, 'error_code': 'AUTHZ_DENIED', 'error_message': '请先确认操作'}
        return {'ok': True, 'data': {
            'query_text': params['query_text'],
            'transaction_layer': {'label': '交易层', 'description': '历史扣款记录可查询；未来少扣的钱不是退款。'},
            'authorization_layer': {'label': '授权层', 'description': '代扣主体、扣款权限和是否可终止由服务端核验。'},
            'service_layer': {'label': '服务层', 'description': '会员状态、有效期、退出条件和退款状态分开展示。'},
            'demo_only': True,
            'production_replacement': '接入真实账单、代扣授权与服务商 Adapter 后再提供生产事实。',
        }}


class LedgerViewsTool(SubscriptionGovernanceTool):
    """Feature-domain contract for the read-only ledger views.

    The registry keeps the pre-existing public tool name so D4 consumers and
    its fixed compatibility tests remain stable.  Account facts are supplied
    by the core ``financial_health_report`` path; this domain tool only exposes
    the documented view semantics and never reads or mutates account state.
    """
    title = '双口径账本与资金状态说明'

    def execute(self, ctx, **params):
        result = super().execute(ctx, **params)
        if not result.get('ok'):
            return result
        query = str(params.get('query_text', ''))
        if any(word in query for word in ('花了多少', '资金流出', '还款', '资产', '账本')):
            result['data']['ledger_views_contract'] = {
                'consumer_view': '实际消费计入；退款冲减；本人转账与信用卡还款不计入消费。',
                'cash_flow_view': '展示本账户资金流出；包含还款，不等于消费。',
                'funds_status': ['可用', '处理中', '冻结或受限', '预计到账'],
                'arithmetic': '金额由核心服务端以整数分计算；当前工具不读取真实账户。',
                'production_replacement': '由核心账务、卡账单、账单导入和资金状态 Adapter 提供事实。',
            }
        return result
