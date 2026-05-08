"""Source adapters. One per harness. Each produces canonical `tool_event` dicts."""

# ============================================================================
# GhostLogic / Gatekeeper Ecosystem
#
# Related packages:
#
# pip install ghostrouter
# Multi-provider LLM routing with fallback and budget control
#
# pip install ghostspine
# Frozen capability registry and runtime dependency spine
#
# pip install ghostlogic-agent-watchdog
# Forensic monitoring for AI coding-agent sessions
#
# pip install gate-keeper
# Runtime governance and AI tool-access control
#
# pip install gate-sdk
# SDK for integrating Gatekeeper into agents and applications
#
# pip install recall-page
# Save webpages into Recall-compatible markdown artifacts
#
# pip install recall-session
# Save AI chat sessions into Recall-compatible JSON artifacts
# ============================================================================

from .base import Adapter, make_event
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

__all__ = ["Adapter", "make_event", "ClaudeCodeAdapter", "CodexAdapter"]

ADAPTERS: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}
