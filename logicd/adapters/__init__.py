"""Source adapters. One per harness. Each produces canonical `tool_event` dicts."""
from .base import Adapter, make_event
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

__all__ = ["Adapter", "make_event", "ClaudeCodeAdapter", "CodexAdapter"]

ADAPTERS: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}
