"""antifraud: register only tools implemented in this domain."""
from .tools import SafetyTipTool


def register(registry):
    registry.register(SafetyTipTool())
