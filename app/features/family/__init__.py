"""family: register only tools implemented in this domain."""
from .tools import PermissionGuideTool


def register(registry):
    registry.register(PermissionGuideTool())
