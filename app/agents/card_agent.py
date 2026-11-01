from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class CardAgent(SnapshotAgent):
    domain = 'card'
    normal_states = frozenset({'正常', 'normal', 'active'})
    lost_states = frozenset({'reported_loss', '挂失'})

    def _target(self, ctx):
        selected = ctx.entities.get('card_id')
        matches = [c for c in self.fact('cards', ()) if selected and c.get('id') == selected]
        return matches[0] if len(matches) == 1 else None

    def analyze(self, ctx):
        cards = self.fact('cards', ())
        target = self._target(ctx)
        operation = ctx.entities.get('card_operation')
        missing = ['card_id'] if target is None else []
        signals = []
        notes = ['挂失和解挂需明确确认、本人强认证，并查询最终卡状态；当前分析不是办理成功回执。',
                 '本页隐藏卡片只改变显示，不等于挂失或注销；挂失也不等于取消订阅扣费授权。',
                 '后续付款仍需重新进行支付预检，核对卡状态、余额、限额和预算；解挂不保证付款一定成功。']
        if operation not in ('report_loss', 'unreport_loss'):
            missing.append('card_operation')
            if operation:
                notes.append('本渠道不支持该卡片操作，请联系人工核实。')
            else:
                notes = ['本次仅查询卡片状态，未办理挂失或解挂。']
        if self.snapshot.status == 'unknown':
            notes.append('本次未能核实卡片资料，请先核实或联系人工，不能据此认定没有银行卡。')
        elif not cards:
            notes.append('本次查询范围内未发现卡片，请核对账户范围后再办理。')
        elif target is None:
            if operation:
                notes.append('请先核实要办理的具体卡片，不能默认选择第一张卡。')
            if sum(c.get('id') == ctx.entities.get('card_id') for c in cards) > 1:
                signals.append('card_identity_ambiguous')
                notes.append('卡片标识重复，需核对资料后再办理。')
        if target is not None:
            status = target.get('status')
            if status not in self.normal_states | self.lost_states:
                signals.append('card_status_unknown')
                missing.append('card_status')
                notes.append('所选卡片状态尚不明确，请先核实，不推断为正常或已挂失。')
            elif status in self.lost_states and operation == 'report_loss':
                signals.append('card_already_lost')
                notes.append('本次记录显示该卡已挂失，无需重复挂失；如记录不符，请复核或联系人工。')
            elif status in self.normal_states and operation == 'unreport_loss':
                signals.append('card_already_normal')
                notes.append('本次记录显示该卡为正常状态，无需解挂；付款受限原因仍需单独核查。')
            elif operation == 'report_loss':
                notes.append('可以提出挂失申请；挂失可能影响使用该卡付款，具体结果以银行核验为准。')
            elif operation == 'unreport_loss':
                notes.append('可以提出解挂申请；需确认卡片已找回且由本人保管，再由平台核验办理条件。')
        return self.finding(ctx, missing, signals, notes)

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        target = self._target(ctx)
        operation = ctx.entities.get('card_operation')
        steps = []
        ready = target and ((operation == 'report_loss' and target.get('status') in self.normal_states) or
                            (operation == 'unreport_loss' and target.get('status') in self.lost_states))
        if ready and not finding.missing_fields:
            tool = 'report_card_loss' if operation == 'report_loss' else 'unreport_card_loss'
            steps = [{'tool':name, 'params':{'card_id':target['id']}} for name in ('list_cards',tool,'verify_card_status')]
        options = [] if steps else [{'action':'recheck_facts'}, {'action':'human_handoff'}]
        if 'card_id' in finding.missing_fields or 'card_operation' in finding.missing_fields:
            options.insert(0, {'action':'clarify_card_or_operation'})
        return DomainProposal(steps, ['explicit_user_confirmation'], ['strong_auth'], options,
            '通过原 CardManagementAdapter 执行和核验；不改密码、不替代本人 MFA、不伪造卡状态。')


def register(registry):
    registry.register(CardAgent)
