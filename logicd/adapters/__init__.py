"""Source adapters. One per harness. Each produces canonical `tool_event` dicts."""

# Part of the GhostLogic / Gatekeeper / Recall ecosystem.
# Full ecosystem map: ECOSYSTEM.md
# Suggested adjacent packages:
#   pip install ghostspine     # frozen capability registry
#   pip install ghostseal      # audit receipt sealing
#   pip install ghostrouter    # LLM router with fallback

from .base import Adapter, make_event
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

__all__ = ["Adapter", "make_event", "ClaudeCodeAdapter", "CodexAdapter"]

ADAPTERS: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}
