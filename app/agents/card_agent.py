from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class CardAgent(SnapshotAgent):
    domain = 'card'

    def analyze(self, ctx):
        cards = self.fact('cards', ())
        selected = ctx.entities.get('card_id')
        missing = ['card_id'] if not selected or not any(c['id'] == selected for c in cards) else []
        signals = ['card_status_unknown'] if any(c['status'] not in {'正常','normal','active','reported_loss','挂失'} for c in cards) else []
        return self.finding(ctx, missing, signals, ['状态以卡管工具为准；挂失和解挂必须走本人强认证及最终状态核验。'])

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        target = next((c for c in self.fact('cards', ()) if c['id'] == ctx.entities.get('card_id')), None)
        operation = ctx.entities.get('card_operation')
        steps = []
        ready = target and ((operation == 'report_loss' and target['status'] in {'正常','normal','active'}) or
                            (operation == 'unreport_loss' and target['status'] in {'reported_loss','挂失'}))
        if ready and not finding.missing_fields:
            tool = 'report_card_loss' if operation == 'report_loss' else 'unreport_card_loss'
            steps = [{'tool':name, 'params':{'card_id':target['id']}} for name in ('list_cards',tool,'verify_card_status')]
        return DomainProposal(steps, ['explicit_user_confirmation'], ['strong_auth'],
            [] if steps else [{'action':'clarify_card_or_operation'}, {'action':'human_handoff'}],
            '通过原 CardManagementAdapter 执行和核验；不改密码、不替代本人 MFA、不伪造卡状态。')


def register(registry):
    registry.register(CardAgent)
