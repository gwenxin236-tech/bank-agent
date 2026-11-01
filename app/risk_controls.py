"""离线风险特征；只有 PolicyEngine 可以将特征转为权限/暂缓决策。"""
from datetime import datetime, timedelta

from policy_config import get_policy

POLICY = get_policy()
AMOUNT_THRESHOLD_CENTS = POLICY["risk_amount_threshold_cents"]
AML_LOWER_CENTS = POLICY["aml_lower_cents"]


def score_transaction_risk(amount_cents, is_new_recipient, keyword_hit, is_elderly, recent_transfer_count):
    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValueError('amount_cents must be a positive integer')
    if type(recent_transfer_count) is not int or recent_transfer_count < 0:
        raise ValueError('recent_transfer_count must be a non-negative integer')
    factors = []
    for hit, code, points, reason in (
        (is_new_recipient, 'new_recipient', POLICY['risk_new_recipient_points'], '新收款人'),
        (amount_cents >= AMOUNT_THRESHOLD_CENTS, 'amount_threshold', POLICY['risk_amount_points'], f'金额达到{AMOUNT_THRESHOLD_CENTS}分'),
        (keyword_hit, 'scam_phrase', POLICY['risk_keyword_points'], '命中本地诈骗话术库'),
        (is_elderly, 'elderly_user', POLICY['risk_elderly_points'], '受信用户档案标记为老年用户'),
        (recent_transfer_count >= POLICY['risk_rapid_transfer_count'] - 1, 'rapid_transfers', POLICY['risk_rapid_points'], f"{POLICY['risk_recent_window_minutes']}分钟内第{POLICY['risk_rapid_transfer_count']}笔及后续转账"),
    ):
        if hit:
            factors.append({'code': code, 'points': points, 'reason': reason})
    score = sum(f['points'] for f in factors)
    return {'risk_score': score, 'risk_factors': factors,
            'decision_reason': '风险分%d；%s' % (score, '；'.join('%s+%d' % (f['reason'], f['points']) for f in factors) or '未命中加分规则')}


def assess_transfer_risk(bank, entities, actor_id):
    """使用账户、实名档案替身和持久账务；不接受客户端提供的风险分/年龄/次数。"""
    now = bank.risk_clock()
    cutoff = now - timedelta(minutes=POLICY["risk_recent_window_minutes"])
    with bank.lock:
        rows = bank.db.execute(
            "SELECT amount_cents, created_at FROM core_transactions WHERE actor_id=? AND status IN ('succeeded','processing')",
            (actor_id,),
        ).fetchall()
        recent, near_threshold = 0, 0
        for row in rows:
            when = datetime.fromisoformat(row['created_at'])
            if when > now:
                continue
            if when >= cutoff:
                recent += 1
            if when.astimezone(now.tzinfo).date() == now.date() and AML_LOWER_CENTS <= row['amount_cents'] < AMOUNT_THRESHOLD_CENTS:
                near_threshold += 1
        amount = entities['amount_cents']
        result = score_transaction_risk(
            amount, bool(entities.get('beneficiary')) and entities['beneficiary'] not in bank.beneficiaries,
            bool(entities.get('fraud_keyword_hit')), bool(bank.user_profiles.get(actor_id, {}).get('is_elderly')),
            recent,
        )
    candidate_near = AML_LOWER_CENTS <= amount < AMOUNT_THRESHOLD_CENTS
    aml_count = near_threshold + int(candidate_near)
    suspect = candidate_near and aml_count >= POLICY["aml_transfer_count"]
    result.update(recent_transfer_count=recent, aml_count=aml_count, aml_suspect=suspect,
                  aml_lower_cents=AML_LOWER_CENTS, aml_threshold_cents=AMOUNT_THRESHOLD_CENTS,
                  history_statuses=['succeeded', 'processing'], assessed_at=now.isoformat(), semantic_evidence=entities.get('_semantic_risk',{}))
    if suspect:
        result['decision_reason'] += f'；疑似结构化拆分：同一用户当日第{aml_count}笔{AML_LOWER_CENTS}分至不足{AMOUNT_THRESHOLD_CENTS}分转账'
    return result
