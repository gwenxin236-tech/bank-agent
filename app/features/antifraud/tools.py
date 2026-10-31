"""Educational notice only; never replaces fraud scoring or cooling-off."""
from tool_contract import BaseTool


class SafetyTipTool(BaseTool):
    name = 'antifraud_safety_tip'
    title = '反诈安全提醒'
    authz_required = True

    def execute(self, ctx, **params):
        if not ctx.authorized:
            return {'ok': False, 'data': {}, 'error_code': 'AUTHZ_DENIED', 'error_message': '请先确认操作'}
        return {'ok': True, 'data': {'message': '请先不要转账。要求把钱转入“安全账户”是危险信号，请通过银行官方客服核实；本示例没有转账，也没有通知外部机构。', 'demo_only': True}}
