"""Privacy filter: payload stripping, redaction, exclude paths."""
from __future__ import annotations
from pathlib import Path

import pytest

from logicd.config import Config, PrivacyConfig, WatchEntry
from logicd.redact import Redactor
from logicd.watcher import Forwarder


def _cfg(tmp_path: Path, *, privacy: PrivacyConfig | None = None) -> Config:
    return Config(
        api_url="https://test.invalid",
        api_key="gl_agent_test",
        state_dir=tmp_path / "state",
        audit_log=tmp_path / "audit.log",
        tick_seconds=600,
        window_days=7,
        max_concurrent_posts=1,
        watches=(),
        privacy=privacy or PrivacyConfig(),
        heartbeat_seconds=0,
    )


def _evt(file_path: str = "x.jsonl", payload: dict | None = None) -> dict:
    return {
        "event_id": "e1", "file_path": file_path, "line_number": 1,
        "byte_offset": 0, "byte_end": 1, "byte_length": 1,
        "sha256": "0" * 64, "captured_at_ns": 0, "tick_index": 0,
        "source": "claude-code", "event_type": "tool_event",
        "payload": payload,
    }


# ------------------------------------------------------------------
# Default: include_payload=False → payload stripped
# ------------------------------------------------------------------

def test_payload_stripped_by_default(tmp_path: Path):
    fwd = Forwarder(_cfg(tmp_path))
    e = _evt(payload={"role": "user", "content": "hello secret content"})
    fwd._apply_privacy(e)
    assert e["payload"] is None
    assert e["_payload_stripped"] is True


def test_payload_kept_when_opted_in(tmp_path: Path):
    cfg = _cfg(tmp_path, privacy=PrivacyConfig(include_payload=True))
    fwd = Forwarder(cfg)
    e = _evt(payload={"role": "user", "content": "hello"})
    fwd._apply_privacy(e)
    assert e["payload"] == {"role": "user", "content": "hello"}
    assert "_payload_stripped" not in e


# ------------------------------------------------------------------
# Redaction (only fires when include_payload=True)
# ------------------------------------------------------------------

def test_redactor_strips_openai_keys():
    r = Redactor((r"sk-[A-Za-z0-9_\-]{16,}",))
    out = r.redact_text("here is sk-abc1234567890XYZdef and more text")
    assert "sk-abc" not in out
    assert "[REDACTED:0]" in out


def test_redactor_strips_jwts():
    r = Redactor((r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",))
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36"
    assert "[REDACTED:0]" in r.redact_text(f"token={jwt}")


def test_redactor_strips_aws_access_keys():
    r = Redactor((r"AKIA[0-9A-Z]{16}",))
    assert "[REDACTED:0]" in r.redact_text("AKIA1234567890ABCDEF")


def test_redact_payload_keeps_shape_when_safe():
    r = Redactor((r"sk-[A-Za-z0-9_\-]{16,}",))
    out = r.redact_payload({"msg": "use sk-abc1234567890DEFGHI today"})
    assert isinstance(out, dict)
    assert "[REDACTED:0]" in out["msg"]


def test_payload_redacted_when_included(tmp_path: Path):
    cfg = _cfg(tmp_path, privacy=PrivacyConfig(
        include_payload=True,
        # Disable defaults so we test only our pattern
        include_default_redactions=False,
        redact_patterns=(r"sk-[A-Za-z0-9_\-]{16,}",),
    ))
    fwd = Forwarder(cfg)
    e = _evt(payload={"text": "use sk-abc1234567890DEFGHI"})
    fwd._apply_privacy(e)
    assert "[REDACTED:0]" in e["payload"]["text"]
    assert "sk-abc" not in e["payload"]["text"]


def test_default_redactions_active_when_payload_included(tmp_path: Path):
    cfg = _cfg(tmp_path, privacy=PrivacyConfig(include_payload=True))
    fwd = Forwarder(cfg)
    e = _evt(payload={"a_jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.aaaaa",
                       "an_openai_key": "sk-abc1234567890DEFGHIJ",
                       "an_aws_key": "AKIA1234567890ABCDEF"})
    fwd._apply_privacy(e)
    serialized = str(e["payload"])
    assert "sk-abc" not in serialized
    assert "AKIA1234567890ABCDEF" not in serialized
    assert "eyJhbGciOiJIUzI1NiJ9" not in serialized


# ------------------------------------------------------------------
# Exclude paths
# ------------------------------------------------------------------

def test_exclude_paths_drops_matching(tmp_path: Path):
    cfg = _cfg(tmp_path, privacy=PrivacyConfig(
        exclude_paths=("**/secret-project/**",),
    ))
    fwd = Forwarder(cfg)
    assert fwd._is_excluded("C:/work/secret-project/.claude/projects/x.jsonl")
    assert fwd._is_excluded("/home/u/secret-project/.claude/sessions/y.jsonl")
    assert not fwd._is_excluded("/home/u/normal/.claude/projects/z.jsonl")


def test_exclude_paths_respects_windows_separators(tmp_path: Path):
    cfg = _cfg(tmp_path, privacy=PrivacyConfig(
        exclude_paths=("**/sensitive/**",),
    ))
    fwd = Forwarder(cfg)
    assert fwd._is_excluded(r"C:\Users\me\sensitive\session.jsonl")
