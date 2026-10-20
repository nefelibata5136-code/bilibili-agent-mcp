"""Full-file Bilibili perception, mounted in the existing per-life MCP server."""
from .engine import WatchService, WatchError, register

__all__ = ["WatchService", "WatchError", "register"]
