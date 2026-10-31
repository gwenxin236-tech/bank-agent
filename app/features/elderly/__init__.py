"""elderly: register only tools implemented in this domain."""
from .tools import PlainLanguageTool


def register(registry):
    registry.register(PlainLanguageTool())
