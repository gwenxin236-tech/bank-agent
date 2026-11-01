from domain_agent_contract import DomainProposal
from domain_snapshots import SnapshotAgent


class FinanceHealthAgent(SnapshotAgent):
    domain = 'finance_health'

    def analyze(self, ctx):
        coverage = self.fact('coverage', {})
        signals = ['coverage_incomplete'] if coverage.get('complete') is not True else []
        funding=self.fact('funding_plan',{})
        if funding.get('status') in {'shortfall','deadline_expired','needs_review'}:signals.append('funding_'+funding['status'])
        return self.finding(ctx, signals=signals, recommendations=[
            '消费与资金流出分开展示；还款属于资金流出，不能再次算作消费；预计收入不是可用余额。',
            '当前覆盖范围以工具为准，不推断家庭完整资产，不自动借款或申购。'])

    def propose(self, ctx, finding):
        self.validate_finding(ctx, finding)
        return DomainProposal([], [], [], [{'action':'review_budget'}, {'action':'human_handoff'}],
            '复用 ledger_views 整数分口径；生产接入已授权多账户账本和覆盖范围核验。')


def register(registry):
    registry.register(FinanceHealthAgent)
