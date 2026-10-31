"""Validated, immutable policy snapshot. Change JSON and restart; never hot-reload a task.

BANK_AGENT_POLICY_PATH may select a deployment/test file. There are no numeric
fallbacks here. Production replacement: reviewed/versioned policy distribution.
The preflight path uses load_policy directly to report failures without starting
any business service. Error messages contain field names, never supplied values.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
from types import MappingProxyType

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / 'config/policy.json'
INTEGER_FIELDS = frozenset('''daily_transfer_limit_cents small_transfer_limit_cents
max_amount_cents birthday_lock_amount_cents idle_cash_threshold_cents family_small_transfer_max_cents
fraud_new_recipient_threshold_cents cooling_off_hours risk_amount_threshold_cents
risk_new_recipient_points risk_amount_points risk_keyword_points risk_elderly_points
risk_rapid_points risk_recent_window_minutes risk_rapid_transfer_count risk_defer_score
aml_lower_cents aml_transfer_count consent_ttl_seconds strong_auth_ttl_seconds
auth_max_attempts breaker_failure_threshold http_rate_limit http_rate_window_seconds
http_max_body_bytes retry_max_attempts retry_base_delay_ms retry_max_delay_ms
preflight_security_buffer_cents'''.split())
LIST_FIELDS = frozenset({'principles', 'green', 'yellow', 'red'})
REQUIRED_FIELDS = INTEGER_FIELDS | LIST_FIELDS | {'version', 'demo_otp'}


class PolicyConfigError(ValueError):
    pass


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyConfigError('策略配置包含重复字段')
        result[key] = value
    return result


def load_policy(path=None):
    try:
        data = json.loads(Path(path or os.getenv('BANK_AGENT_POLICY_PATH') or DEFAULT_POLICY_PATH).read_text(encoding='utf-8'),
                          object_pairs_hook=_unique_keys)
    except (OSError, ValueError, TypeError) as exc:
        raise PolicyConfigError('无法读取有效策略配置') from None
    if not isinstance(data, dict) or set(data) != REQUIRED_FIELDS:
        raise PolicyConfigError('策略配置缺少必填字段或包含未知字段')
    for key in INTEGER_FIELDS:
        if type(data[key]) is not int or data[key] <= 0:
            raise PolicyConfigError('策略字段必须是正整数: ' + key)
    if data['max_amount_cents'] > 9007199254740991:
        raise PolicyConfigError('金额超出整数安全表示范围')
    for key in LIST_FIELDS:
        values = data[key]
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(set(values)) != len(values)):
            raise PolicyConfigError('策略字段必须是非空且无重复的字符串列表: ' + key)
    if not isinstance(data['version'], str) or not data['version'].strip():
        raise PolicyConfigError('策略版本无效')
    otp = data['demo_otp']
    if not isinstance(otp, str) or len(otp) != 6 or not otp.isascii() or not otp.isdigit():
        raise PolicyConfigError('演示认证配置格式无效')
    for lower, upper in (
        ('small_transfer_limit_cents', 'daily_transfer_limit_cents'),
        ('daily_transfer_limit_cents', 'max_amount_cents'),
        ('birthday_lock_amount_cents', 'max_amount_cents'),
        ('idle_cash_threshold_cents', 'max_amount_cents'),
        ('family_small_transfer_max_cents', 'small_transfer_limit_cents'),
        ('risk_amount_threshold_cents', 'max_amount_cents'),
        ('fraud_new_recipient_threshold_cents', 'max_amount_cents'),
        ('strong_auth_ttl_seconds', 'consent_ttl_seconds'),
        ('retry_base_delay_ms', 'retry_max_delay_ms'),
    ):
        if data[lower] > data[upper]:
            raise PolicyConfigError('策略上下界冲突: ' + lower + '/' + upper)
    if data['aml_lower_cents'] >= data['risk_amount_threshold_cents']:
        raise PolicyConfigError('AML 下界必须低于风险金额阈值')
    if data['aml_transfer_count'] < 2 or data['risk_rapid_transfer_count'] < 2:
        raise PolicyConfigError('连续转账规则至少需要两笔')
    # Match the contest's three-color table: subscription queries are read-only
    # but still require explicit consent. Tool side effects do not define risk.
    automatic_queries = {'balance_query', 'transaction_query', 'bill_analysis', 'investment_query', 'financial_health'}
    if set(data['green']) != automatic_queries or set(data['yellow']) != {'transfer', 'subscription_query', 'subscription_cancel', 'virtual_card_apply'} or set(data['red']) != {'card_manage', 'password_change', 'investment_purchase', 'investment_redemption'}:
        raise PolicyConfigError('权限分组不得移除安全边界或将副作用降为只读')
    return MappingProxyType({k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


@lru_cache(maxsize=1)
def get_policy():
    """One immutable snapshot per process, shared by core/policy/auth/display."""
    return load_policy()
