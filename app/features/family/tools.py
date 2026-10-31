"""Public policy explanation, NOT an authorization grant or account query."""
from tool_contract import BaseTool


class PermissionGuideTool(BaseTool):
    name = 'family_permission_guide'
    title = '家庭授权边界说明'
    authz_required = True

    def execute(self, ctx, **params):
        if not ctx.authorized:
            return {'ok': False, 'data': {}, 'error_code': 'AUTHZ_DENIED', 'error_message': '请先确认操作'}
        return {'ok': True, 'data': {'message': '守护关系不等于账户所有权。查询和限额代办需有效授权；非查询代办仍需本人认证，大额转账、密码和卡片操作不能代办。本示例不读取家庭账户、不授予或修改授权。', 'demo_only': True}}
