from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class SubscriptionAgent(SnapshotAgent):
    domain = 'subscription'

    def _target(self, ctx):
        selected = ctx.entities.get('subscription_id')
        matches = [i for i in self.fact('subscriptions', ()) if selected and i.get('id') == selected]
        return matches[0] if len(matches) == 1 else None

    def analyze(self, ctx):
        items = self.fact('subscriptions', ())
        target = self._target(ctx)
        missing = ['subscription_id'] if target is None else []
        signals = []
        notes = ['历史扣款、未来扣费、服务有效期和退款分别核验；停止扣费不会撤销历史交易。',
                 '节省的未来费用和待核验退款不能计入当前可用余额；资金安排仍需核对账本和预算。']
        if self.snapshot.status == 'unknown':
            notes.append('本次未能核实订阅资料，请重新核对或联系人工；不能据此认定没有订阅。')
        elif not items:
            notes.append('本次查询范围内未发现订阅；这不代表其他账户或服务商没有扣费。')
        elif target is None:
            notes.append('请从本次查询的订阅中选择具体项目，避免取消错服务。')
            if sum(i.get('id') == ctx.entities.get('subscription_id') for i in items) > 1:
                signals.append('subscription_identity_ambiguous')
                notes.append('订阅标识重复，需核对资料后再办理。')
        relevant = (target,) if target is not None else items
        if any(i.get('refund_state', 'unknown') == 'unknown' for i in relevant):
            signals.append('refund_unverified')
            notes.append('退款是否到账尚未核实，不能把退款金额为零理解为无需退款或已经到账。')
        if target is not None:
            debit = target.get('debit_state')
            if debit == 'stopped':
                signals.append('debit_already_stopped')
                notes.append('本次记录显示未来扣费已停止，无需重复取消；服务和退款仍需分别查看。')
            elif debit != 'active':
                missing.append('debit_state')
                signals.append('debit_state_unknown')
                notes.append('未来扣费是否仍开启尚未核实，先核对扣费授权。')
            elif target.get('terminable') is not True:
                signals.append('cancellation_unavailable')
                notes.append('当前资料不支持在此渠道取消，请联系服务商或人工核实办理方式。')
            else:
                notes.append('可以提出停止后续自动扣费的申请，需确认后由平台办理；当前尚未取消。')
            service = target.get('service_state')
            notes.append({'active':'记录中的服务状态为有效；停止扣费不等于服务立即结束，有效期以服务商核验为准。',
                          'expired':'记录中的服务已到期；服务到期不等于扣费授权已停止。'}.get(
                              service, '服务有效状态需向服务商核实，不能根据扣费状态推断。'))
            refund = target.get('refund_state')
            if refund == 'pending':
                signals.append('refund_pending')
                notes.append('退款记录为处理中，到账时间和可用资金仍需核验。')
            elif refund != 'unknown':
                notes.append('退款记录需与到账流水核对；本域不凭服务状态确认资金到账。')
        return self.finding(ctx, missing, signals, notes)

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        items = self.fact('subscriptions', ())
        target = self._target(ctx)
        steps = []
        if (not finding.missing_fields and ctx.entities.get('operation') == 'cancel' and target
                and target['terminable'] and target['debit_state'] == 'active'):
            steps = [{'tool':'cancel_subscription', 'params':{'subscription_id':target['id']}}]
        options = []
        if 'subscription_id' in finding.missing_fields and self.snapshot.status == 'known':
            options = [{'action':'select_subscription', 'subscription_id':i['id'], 'label':i['merchant'] or '待核验订阅'}
                       for i in items if isinstance(i.get('id'), str) and i['id'].strip()
                       and sum(other.get('id') == i['id'] for other in items) == 1]
        if (self.snapshot.status == 'unknown' or (finding.missing_fields and not options) or
                'cancellation_unavailable' in finding.risk_signals):
            options.extend([{'action':'recheck_facts'}, {'action':'human_handoff'}])
        return DomainProposal(steps, ['explicit_user_confirmation'], [], options,
            '生产接入账单、代扣和服务商 Adapter；查询和取消均经现有黄色确认，不直接取消或退款。')


def register(registry):
    registry.register(SubscriptionAgent)
