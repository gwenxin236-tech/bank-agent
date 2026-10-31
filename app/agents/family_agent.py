from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class FamilyAgent(SnapshotAgent):
    domain = 'family'

    def analyze(self, ctx):
        grant = self.fact('grant')
        missing = ['grant_id'] if ctx.actor_id != ctx.subject_id and grant is None else []
        signals = ['grant_inactive'] if grant and not grant.get('active_at_capture') else []
        return self.finding(ctx, missing, signals, ['只报告关系、授权类型、额度和有效期事实；亲属称呼不是操作权限。'])

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        auth = ['owner_verification'] if ctx.actor_id != ctx.subject_id else []
        return DomainProposal([], [], auth, [{'action':'review_owner_grant'}, {'action':'human_handoff'}],
            '生产替换为被守护人正式授权；最终权限、额度占用和本人认证由平台判定。')


def register(registry):
    registry.register(FamilyAgent)
