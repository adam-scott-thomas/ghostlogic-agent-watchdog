"""401/403 from the server triggers a hard pause, not silent dead-letter.

Silent dead-lettering on auth failure was the v0.1.0 audit blocker — the
queue would grow forever while the operator wondered why no events were
landing on the server. Now: emit `key_revoked_or_unauthorized` to the
audit log, raise `KeyRevokedOrUnauthorized`, propagate up so the service
runtime exits non-zero."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from logicd.audit import AuditLog
from logicd.client import IngestClient, KeyRevokedOrUnauthorized


def _event(eid: str = "e1") -> dict:
    return {"event_id": eid, "captured_at_ns": 0, "tick_index": 0,
            "source": "claude-code", "file_path": "x", "line_number": 1,
            "byte_offset": 0, "byte_end": 1, "sha256": "0" * 64,
            "event_type": "tool_event"}


def _resp_factory(status: int):
    class _R:
        def __init__(self):
            self.status = status
        async def text(self): return f"server says {status}"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
    def _post(url, json, headers):
        return _R()
    return _post


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_fatal_status_raises_key_revoked(tmp_path: Path, status: int):
    audit = AuditLog(tmp_path / "audit.log")
    client = IngestClient(
        api_url="https://example.invalid", api_key="gl_agent_test",
        audit=audit, dead_letter_dir=tmp_path / "dl",
    )
    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_resp_factory(status))
    fake_session.close = AsyncMock()
    client._session = fake_session

    with pytest.raises(KeyRevokedOrUnauthorized) as excinfo:
        await client.ship([_event()], hostname="testhost")
    assert excinfo.value.status == status

    # And: nothing was dead-lettered (this was the bug — the queue would
    # silently fill with 401s instead of pausing).
    dl_files = list((tmp_path / "dl").glob("*.json"))
    assert dl_files == []


@pytest.mark.asyncio
async def test_auth_fatal_emits_audit_event(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.log")
    client = IngestClient(
        api_url="https://example.invalid", api_key="gl_agent_test",
        audit=audit, dead_letter_dir=tmp_path / "dl",
    )
    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_resp_factory(401))
    fake_session.close = AsyncMock()
    client._session = fake_session

    with pytest.raises(KeyRevokedOrUnauthorized):
        await client.ship([_event()], hostname="testhost")

    log_text = (tmp_path / "audit.log").read_text()
    assert "key_revoked_or_unauthorized" in log_text


@pytest.mark.asyncio
async def test_other_4xx_still_dead_letters(tmp_path: Path):
    """422/400 etc. are still terminal-but-park (not auth-fatal)."""
    audit = AuditLog(tmp_path / "audit.log")
    dead_letter = tmp_path / "dl"
    client = IngestClient(
        api_url="https://example.invalid", api_key="gl_agent_test",
        audit=audit, dead_letter_dir=dead_letter,
    )
    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_resp_factory(422))
    fake_session.close = AsyncMock()
    client._session = fake_session

    # 422 should NOT raise KeyRevokedOrUnauthorized — it dead-letters.
    await client.ship([_event()], hostname="testhost")
    dl_files = list(dead_letter.glob("*.json"))
    assert len(dl_files) == 1
