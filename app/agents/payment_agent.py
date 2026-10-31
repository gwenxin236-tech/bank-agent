from transaction_agent import TransactionAgent


class PaymentAgent(TransactionAgent):
    """Reuse the T10 payment implementation without copying its logic."""


def register(registry):
    registry.register(PaymentAgent)
