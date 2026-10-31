from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class SubscriptionAgent(SnapshotAgent):
    domain = 'subscription'

    def analyze(self, ctx):
        items = self.fact('subscriptions', ())
        selected = ctx.entities.get('subscription_id')
        missing = ['subscription_id'] if not selected or not any(i['id'] == selected for i in items) else []
        signals = ['refund_unverified'] if any(i['refund_state'] == 'unknown' for i in items) else []
        return self.finding(ctx, missing, signals, ['历史扣款、停止未来扣费、服务有效期和退款分别核验；未知退款不能说已到账。'])

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        items = self.fact('subscriptions', ())
        target = next((i for i in items if i['id'] == ctx.entities.get('subscription_id')), None)
        steps = []
        if (not finding.missing_fields and ctx.entities.get('operation') == 'cancel' and target
                and target['terminable'] and target['debit_state'] == 'active'):
            steps = [{'tool':'cancel_subscription', 'params':{'subscription_id':target['id']}}]
        options = [{'action':'select_subscription', 'subscription_id':i['id'], 'label':i['merchant'] or '待核验订阅'} for i in items] if finding.missing_fields else []
        return DomainProposal(steps, ['explicit_user_confirmation'], [], options,
            '生产接入账单、代扣和服务商 Adapter；查询和取消均经现有黄色确认，不直接取消或退款。')


def register(registry):
    registry.register(SubscriptionAgent)
