"""finance_health: register only tools implemented in this domain."""
from .tools import BudgetTool, LedgerViewsTool


def register(registry):
    registry.register(BudgetTool())
    registry.register(LedgerViewsTool())
