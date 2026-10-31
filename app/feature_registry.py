"""Domain-owned registration and narrow adapters for the existing Agent pipeline.

Only reviewed local packages are imported. Registration does not grant bank
permissions. Atomic startup publication preserves the core registry on failure.
"""
import copy
import importlib
import json
from pathlib import Path
import re
from types import MappingProxyType

from tool_contract import BaseTool, ToolContext, validate_params, validate_result

FEATURE_INTENT = 'feature_demo'
CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config/features.json'
PACKAGE_ROOT = Path(__file__).resolve().parent / 'features'
UNKNOWN_TOOL = 'feature_unregistered'


class _DomainRegistrar:
    """A domain gets only its scoped registration interface, not the central dict."""
    def __init__(self, domain, reserved, staged):
        self._domain, self._reserved, self._staged = domain, reserved, staged

    def register(self, tool):
        if not isinstance(tool, BaseTool):
            raise ValueError('域工具必须实现 BaseTool')
        name = getattr(tool, 'name', '')
        if (type(name) is not str or not re.fullmatch(re.escape(self._domain) + r'_[a-z][a-z0-9_]*', name) or
                not type(tool).__module__.startswith(f'features.{self._domain}.')):
            raise ValueError('工具名称或实现不属于当前域，禁止越域注册')
        if name in self._reserved or name in self._staged or name == UNKNOWN_TOOL:
            raise ValueError('工具重名；禁止覆盖内置或已注册工具')
        if type(tool.authz_required) is not bool or type(tool.side_effect) is not bool:
            raise ValueError('工具权限/副作用元数据必须是布尔值')
        if tool.side_effect:
            raise ValueError('D4 只开放无副作用工具；副作用需负责人接入核心策略、幂等和恢复')
        if not isinstance(tool.title, str) or not tool.title.strip():
            raise ValueError('工具必须声明可读标题')
        if type(tool.parameters) is not dict or any(
                not isinstance(k, str) or k.startswith('_') or
                type(v) is not str or v not in {'text', 'cents'} or (v == 'cents' and not k.endswith('_cents')) or
                (k.endswith('_cents') and v != 'cents')
                for k, v in tool.parameters.items()):
            raise ValueError('参数契约必须使用 text / cents 类型与 *_cents 金额名称')
        self._staged[name] = tool


class FeatureRegistry:
    def __init__(self, tools, configs):
        self._tools = MappingProxyType(dict(tools))
        self._configs = copy.deepcopy(configs)

    @property
    def names(self):
        return tuple(self._tools)

    def get(self, name):
        return self._tools.get(name)

    def parse_request(self, text):
        """Explicit offline command only; never let model-selected tools grant access."""
        if not text.strip().startswith('功能演示'):
            return None
        match = re.fullmatch(r'功能演示\s+([a-z][a-z0-9_]*)(?:\s+(.+))?', text.strip(), flags=re.S)
        requested = match.group(1) if match else ''
        options = self._configs.get(requested, {})
        name = options.get('demo_tool', requested)
        if name not in self._tools:
            # Never hand a built-in tool name through the feature plan adapter.
            return {'feature_tool': UNKNOWN_TOOL, 'requested_tool': requested, 'feature_params': {}}
        defaults = next((c['demo_params'] for c in self._configs.values() if c.get('demo_tool') == name), {})
        params = copy.deepcopy(defaults)
        error = ''
        try:
            if match.group(2) is not None:
                params = json.loads(match.group(2))
            validate_params(self._tools[name].parameters, params)
        except (ValueError, TypeError):
            # Do not persist invalid floats, identity claims or malformed JSON in a plan.
            params, error = {}, '示例参数无效，请使用工具说明中的字段，金额必须为整数分'
        return {'feature_tool': name, 'requested_tool': requested, 'feature_params': params,
                'feature_error': error}

    def describe_request(self, entities):
        name = entities.get('feature_tool')
        tool = self.get(name)
        if tool is None:
            return '未找到已注册的域工具，不能执行', UNKNOWN_TOOL
        return f'{tool.title}（离线示例，不操作账户；结果见服务记录）', name

    def execute(self, runtime, plan, step, decision):
        """Called only after the core PolicyEngine.classify_tool gate.

        Recheck confirmation/strong-auth using server state, never a model flag.
        Return a dict matching existing ToolResult; core owns audit and lifecycle.
        """
        def failure(code, message, denied=False):
            if denied:
                runtime.audit.write(plan.trace_id, 'tool.denied', {'tool': step.tool, 'reason': code},
                                    actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            return {'ok': False, 'tool': step.tool, 'data': {}, 'error_code': code,
                    'error_message': message, 'trace_id': plan.trace_id}

        tool = self.get(step.tool)
        if tool is None:
            return failure('UNKNOWN_TOOL', '未注册的工具，已拒绝调用', True)
        if (plan.intent != FEATURE_INTENT or plan.entities.get('feature_tool') != step.tool or
                decision.get('authz_allowed') is False or decision.get('defer') or
                not plan.actor_id or not plan.device_id):
            return failure('AUTHZ_DENIED', '当前计划或策略不允许执行此域工具', True)
        confirmed = (plan.plan_id in runtime.executing and plan.consent_token in runtime.consumed_tokens)
        if (tool.authz_required or plan.requires_confirmation or decision.get('requires_confirmation')) and not confirmed:
            return failure('CONSENT_REQUIRED', '请先核对域工具计划，再确认执行', True)
        if decision.get('requires_strong_auth') and plan.plan_id not in getattr(runtime, '_execution_authorized', set()):
            return failure('REAUTH_REQUIRED', '当前策略要求本人强认证，不能由工具或模型跳过', True)
        try:
            if plan.entities.get('feature_error'):
                raise ValueError('invalid request')
            params = copy.deepcopy(plan.entities.get('feature_params'))
            validate_params(tool.parameters, params)
        except (ValueError, TypeError):
            return failure('INVALID_FEATURE_PARAMS', '示例参数无效，金额必须为整数分且不能声明权限', True)
        family = plan.entities.get('_family', {})
        ctx = ToolContext(plan.trace_id, plan.plan_id, step.id, family.get('guardian_id', plan.actor_id),
                          plan.actor_id, plan.device_id, True)
        try:
            result = tool.execute(ctx, **params)  # One attempt; never retry automatically.
        except Exception:
            # Do not leak exception internals, secrets or stack traces to logs/users.
            return failure('FEATURE_TOOL_ERROR', '域工具执行失败，未自动重试')
        try:
            validate_result(result)
        except (TypeError, ValueError):
            return failure('INVALID_TOOL_RESULT', '域工具返回值不符合结构化契约')
        return dict(copy.deepcopy(result), tool=step.tool, trace_id=plan.trace_id)


def _read_config(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('无法读取 features 配置，拒绝启动扩展注册') from exc
    if type(config) is not dict:
        raise ValueError('features 必须按域配置对象')
    if 'dag' in config:
        option = config['dag']
        if type(option) is not dict or set(option) != {'enabled'} or type(option['enabled']) is not bool:
            raise ValueError('dag 仅允许布尔 enabled 开关，不是工具域')
    return config


def load_dag_enabled(config_path=CONFIG_PATH):
    """Missing switch is legacy-compatible; invalid values never become truthy."""
    return _read_config(config_path).get('dag', {}).get('enabled', False)


def load_features(central_registry, config_path=CONFIG_PATH):
    """Scan only enabled local domain __init__.py; publish after ALL checks pass."""
    config = _read_config(config_path)
    domains = sorted(p.name for p in PACKAGE_ROOT.iterdir()
                     if p.is_dir() and (p / '__init__.py').is_file())
    if set(config) - {'dag'} != set(domains):
        raise ValueError('features 必须按已存在的域分别配置对象，不使用共享数组')
    staged, enabled = {}, {}
    for domain in domains:
        path = PACKAGE_ROOT / domain
        options = config[domain]
        if (not re.fullmatch(r'[a-z][a-z0-9_]*', domain) or path.is_symlink() or
                type(options) is not dict or type(options.get('enabled')) is not bool or
                type(options.get('demo_tool')) is not str or
                set(options) != {'enabled', 'demo_tool', 'demo_params'}):
            raise ValueError('域目录或配置格式不合法')
        if not options['enabled']:
            continue
        module = importlib.import_module(f'features.{domain}')
        if Path(module.__file__).resolve() != (path / '__init__.py').resolve():
            raise ValueError('域模块来源必须是本仓库对应目录')
        register = getattr(module, 'register', None)
        if not callable(register):
            raise ValueError('每个启用域必须提供 register(registry)')
        register(_DomainRegistrar(domain, set(central_registry), staged))
        demo = staged.get(options['demo_tool'])
        if demo is None or not demo.name.startswith(domain + '_'):
            raise ValueError('demo_tool 必须属于本域且已注册')
        validate_params(demo.parameters, options['demo_params'])
        enabled[domain] = options
    central_registry.update({name: {'side_effect': False, 'retry': 0} for name in staged})
    return FeatureRegistry(staged, enabled)
