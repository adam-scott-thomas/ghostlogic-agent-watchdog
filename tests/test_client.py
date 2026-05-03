"""IngestClient HTTP behavior: idempotency, auth, retry boundaries."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from logicd.audit import AuditLog
from logicd.batch import batch_id
from logicd.client import IngestClient


def _make_event(eid: str) -> dict:
    return {"event_id": eid, "captured_at_ns": 0, "tick_index": 0,
            "source": "claude-code", "file_path": "x", "line_number": 1,
            "byte_offset": 0, "byte_end": 1, "sha256": "0" * 64}


@pytest.mark.asyncio
async def test_ship_sends_idempotency_key_matching_batch_id(tmp_path: Path):
    """Server-side dedup gets an explicit Idempotency-Key header equal to the
    deterministic batch_id. Crash-restart retries reuse the same key."""
    audit = AuditLog(tmp_path / "audit.log")
    client = IngestClient(
        api_url="https://example.invalid", api_key="gl_agent_test",
        audit=audit, dead_letter_dir=tmp_path / "dl",
    )
    events = [_make_event("e1"), _make_event("e2")]
    expected_bid = batch_id(events)

    captured = {}

    class _Resp:
        status = 200
        async def text(self): return "ok"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    def _post(url, json, headers):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _Resp()

    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_post)
    fake_session.close = AsyncMock()
    client._session = fake_session

    await client.ship(events, hostname="testhost")

    assert captured["url"] == "https://example.invalid/api/v1/ingest"
    assert captured["headers"]["Authorization"] == "Bearer gl_agent_test"
    assert captured["headers"]["Idempotency-Key"] == expected_bid
    assert captured["body"]["batch_id"] == expected_bid


@pytest.mark.asyncio
async def test_ship_prepared_preserves_original_batch_id_in_idempotency_key(tmp_path: Path):
    """Dead-letter replay reuses the original batch_id; Idempotency-Key must
    follow it so the server keeps deduplicating against the same key."""
    audit = AuditLog(tmp_path / "audit.log")
    client = IngestClient(
        api_url="https://example.invalid", api_key="gl_agent_test",
        audit=audit, dead_letter_dir=tmp_path / "dl",
    )

    captured = {}

    class _Resp:
        status = 200
        async def text(self): return "ok"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    def _post(url, json, headers):
        captured["headers"] = headers
        return _Resp()

    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_post)
    fake_session.close = AsyncMock()
    client._session = fake_session

    original_bid = "a" * 64  # what the original DL file recorded
    body = {"batch_id": original_bid, "events": [_make_event("e1")],
            "source_id": "h", "agent_id": "logicd",
            "endpoint_name": "h/logicd", "timestamp": "now"}
    await client.ship_prepared(body)

    assert captured["headers"]["Idempotency-Key"] == original_bid
