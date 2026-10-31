"""Read-only plain-language sample; no identity or account access."""
from tool_contract import BaseTool


class PlainLanguageTool(BaseTool):
    name = 'elderly_plain_language'
    title = '长辈白话说明'
    authz_required = True
    parameters = {'text': 'text'}

    def execute(self, ctx, **params):
        if not ctx.authorized:
            return {'ok': False, 'data': {}, 'error_code': 'AUTHZ_DENIED', 'error_message': '请先确认操作'}
        terms = {'触发增强认证策略': '为了保护您的钱，请再确认一次', '幂等': '重复点击也只办理一次'}
        message = terms.get(params['text'], '这个说法还没有白话解释，请向银行官方客服核实。')
        return {'ok': True, 'data': {'message': message, 'demo_only': True}}
