from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class AntifraudAgent(SnapshotAgent):
    domain = 'antifraud'

    def analyze(self, ctx):
        factors = self.fact('risk_factors', ())
        signals = [f['code'] for f in factors if isinstance(f.get('code'), str)]
        signals += list(self.fact('signals', ()))
        if self.fact('aml_suspect', False):
            signals.append('aml_pattern')
        return self.finding(ctx, signals=signals, recommendations=[
            '只报告既有风险因素；金额或亲属称呼本身不能证明诈骗。拦截、暂缓、冷静期由平台裁决。'])

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        options = [{'action':'review_risk_factors'}, {'action':'human_handoff'}] if finding.risk_signals else []
        return DomainProposal([], [], [], options, '生产替换可信风险特征源；策略、冻结与通知仍由平台负责。')


def register(registry):
    registry.register(AntifraudAgent)
