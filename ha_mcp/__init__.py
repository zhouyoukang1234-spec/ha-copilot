"""HA-MCP: an MCP server that plugs an external agent into all of Home Assistant."""

from .client import HAClient, HAError

__all__ = ["HAClient", "HAError"]
