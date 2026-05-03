"""Regression tests for the offset-advance-after-durability invariant.

Scenarios:
  A. Successful flush → offsets advance to the max byte_end per file.
  B. Flush raises → events are re-queued and offsets stay put.
  C. Dead-lettered batches are replayed on next startup.
  D. Line numbers are tracked correctly across resume.
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from logicd.config import Config, WatchEntry
from logicd.watcher import Forwarder


def _cfg(tmp_path: Path, watches: tuple[WatchEntry, ...]) -> Config:
    return Config(
        api_url="https://test.invalid",
        api_key="gl_agent_test",
        state_dir=tmp_path / "state",
        audit_log=tmp_path / "audit.log",
        tick_seconds=600,
        window_days=7,
        max_concurrent_posts=1,
        watches=watches,
        heartbeat_seconds=0,  # disable heartbeats in durability tests
    )


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())


@pytest.mark.asyncio
async def test_offset_advances_after_successful_ship(tmp_path: Path):
    src = tmp_path / "src" / "session-01.jsonl"
    _write_jsonl(src, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "bye"},
    ])
    watches = (WatchEntry(name="t", adapter="claude_code", paths=(str(src),)),)
    cfg = _cfg(tmp_path, watches)
    fwd = Forwarder(cfg)

    # Tail the file -> events queued, no offset advance yet
    fwd._pending_files.add(str(src))
    fwd._drain_pending_files()
    assert fwd.queue.count == 3
    assert fwd.state.get_offset(str(src)) == (0, 0)  # NOT advanced yet

    # Simulate a successful ship
    fake_client = MagicMock()
    fake_client.ship = AsyncMock(return_value=None)
    await fwd._flush(fake_client)

    assert fwd.queue.count == 0
    bo, ln = fwd.state.get_offset(str(src))
    assert bo == src.stat().st_size  # advanced to EOF
    assert ln == 3


@pytest.mark.asyncio
async def test_offset_does_not_advance_on_flush_exception(tmp_path: Path):
    src = tmp_path / "src" / "session-02.jsonl"
    _write_jsonl(src, [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}])
    watches = (WatchEntry(name="t", adapter="claude_code", paths=(str(src),)),)
    cfg = _cfg(tmp_path, watches)
    fwd = Forwarder(cfg)

    fwd._pending_files.add(str(src))
    fwd._drain_pending_files()
    assert fwd.queue.count == 2
    pre_offset = fwd.state.get_offset(str(src))

    # Ship raises -> events must go back in queue, offset must not move
    fake_client = MagicMock()
    fake_client.ship = AsyncMock(side_effect=RuntimeError("disk full writing dead letter"))
    await fwd._flush(fake_client)

    assert fwd.queue.count == 2
    assert fwd.state.get_offset(str(src)) == pre_offset


@pytest.mark.asyncio
async def test_dead_letter_replay_preserves_batch_id(tmp_path: Path):
    """Replaying a dead-letter file MUST use the original batch_id, not re-hash
    the events into a new id. Otherwise server-side dedup fails on restart."""
    cfg = _cfg(tmp_path, watches=())

    dl_dir = cfg.state_dir / "dead_letter"
    dl_dir.mkdir(parents=True, exist_ok=True)
    ORIGINAL_BID = "ORIGINAL_BATCH_ID_deadbeefcafef00d"
    dl_payload = {
        "reason": "retries_exhausted",
        "body": {
            "batch_id": ORIGINAL_BID,
            "source_id": "host-a",
            "agent_id": "logicd",
            "endpoint_name": "host-a/logicd",
            "timestamp": "2026-04-20T12:00:00Z",
            "events": [{"event_id": "e-1", "file_path": "x", "byte_offset": 0,
                        "byte_end": 10, "byte_length": 8, "line_number": 1,
                        "sha256": "abc", "payload": {}}],
        },
    }
    dl_file = dl_dir / "1234_abcdef.json"
    dl_file.write_text(json.dumps(dl_payload))

    fwd = Forwarder(cfg)
    fwd._replay_dead_letter(dl_dir)

    # Replay must NOT push to live queue (that would merge batch_ids on flush).
    assert fwd.queue.count == 0
    # Instead, exactly one presealed body with the original batch_id.
    assert len(fwd._presealed) == 1
    body, src = fwd._presealed[0]
    assert body["batch_id"] == ORIGINAL_BID
    assert src == dl_file

    # Simulate a successful re-ship → file archives to replayed/.
    fake_client = MagicMock()
    fake_client.ship_prepared = AsyncMock(return_value=None)
    await fwd._flush_presealed(fake_client)

    assert fake_client.ship_prepared.await_count == 1
    called_with = fake_client.ship_prepared.await_args.args[0]
    assert called_with["batch_id"] == ORIGINAL_BID
    # File moved to replayed/ (not deleted)
    assert not dl_file.exists()
    archived = dl_dir / "replayed" / dl_file.name
    assert archived.exists()
    assert fwd._presealed == []


@pytest.mark.asyncio
async def test_dead_letter_reship_failure_keeps_file(tmp_path: Path):
    """If re-ship fails, the DL file stays in place and gets retried next flush."""
    cfg = _cfg(tmp_path, watches=())

    dl_dir = cfg.state_dir / "dead_letter"
    dl_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "batch_id": "bid-1", "source_id": "h", "agent_id": "logicd",
        "endpoint_name": "h/logicd", "timestamp": "2026-04-20T12:00:00Z",
        "events": [{"event_id": "e", "file_path": "x", "byte_offset": 0,
                    "byte_end": 5, "byte_length": 3, "line_number": 1,
                    "sha256": "a", "payload": {}}],
    }
    dl_file = dl_dir / "1_b.json"
    dl_file.write_text(json.dumps({"reason": "r", "body": body}))

    fwd = Forwarder(cfg)
    fwd._replay_dead_letter(dl_dir)
    assert len(fwd._presealed) == 1

    fake_client = MagicMock()
    fake_client.ship_prepared = AsyncMock(side_effect=RuntimeError("origin down"))
    await fwd._flush_presealed(fake_client)

    # File stays put; presealed entry retained for next flush
    assert dl_file.exists()
    assert not (dl_dir / "replayed" / dl_file.name).exists()
    assert len(fwd._presealed) == 1


@pytest.mark.asyncio
async def test_orchestrate_flushes_presealed_when_live_queue_empty(tmp_path: Path):
    """Regression: the scheduler must call _flush on every tick, not only when
    live events exist. Otherwise presealed dead-letter bodies never replay."""
    cfg = _cfg(tmp_path, watches=())
    fwd = Forwarder(cfg)

    dl_file = tmp_path / "fake_dl.json"
    body = {
        "batch_id": "bid-orch", "source_id": "h", "agent_id": "logicd",
        "endpoint_name": "h/logicd", "timestamp": "2026-04-20T12:00:00Z",
        "events": [{"event_id": "e", "file_path": "x", "byte_offset": 0,
                    "byte_end": 5, "byte_length": 3, "line_number": 1,
                    "sha256": "a", "payload": {}}],
    }
    dl_file.write_text(json.dumps({"reason": "r", "body": body}))
    fwd._presealed = [(body, dl_file)]
    assert fwd.queue.count == 0  # no live events

    fake_client = MagicMock()
    fake_client.ship_prepared = AsyncMock(return_value=None)
    fake_client.ship = AsyncMock(return_value=None)

    # Shrink the flush delay so the test doesn't wait 5s
    from logicd import batch as batch_mod
    orig_idle = batch_mod.IDLE_FLUSH_SECONDS
    batch_mod.IDLE_FLUSH_SECONDS = 0.05
    try:
        async def stop_soon():
            await asyncio.sleep(0.15)  # 3 flush cycles
            fwd.request_stop()
        stopper = asyncio.create_task(stop_soon())
        await fwd._orchestrate(fake_client)
        await stopper
    finally:
        batch_mod.IDLE_FLUSH_SECONDS = orig_idle

    # Key assertion: the orchestrator reached the presealed flush at least once.
    assert fake_client.ship_prepared.await_count >= 1
    called_body = fake_client.ship_prepared.await_args.args[0]
    assert called_body["batch_id"] == "bid-orch"
    # And the live ship was NOT called unnecessarily
    assert fake_client.ship.await_count == 0


@pytest.mark.asyncio
async def test_archive_failure_keeps_slot_for_retry(tmp_path: Path):
    """Regression: when ship succeeds but archive fails, the slot must stay in
    _presealed so archive retries on next flush (not silently dropped)."""
    cfg = _cfg(tmp_path, watches=())
    fwd = Forwarder(cfg)

    dl_dir = cfg.state_dir / "dead_letter"
    dl_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "batch_id": "bid-A", "source_id": "h", "agent_id": "logicd",
        "endpoint_name": "h/logicd", "timestamp": "2026-04-20T12:00:00Z",
        "events": [{"event_id": "e", "file_path": "x", "byte_offset": 0,
                    "byte_end": 5, "byte_length": 3, "line_number": 1,
                    "sha256": "a", "payload": {}}],
    }
    dl_file = dl_dir / "1_a.json"
    dl_file.write_text(json.dumps({"reason": "r", "body": body}))
    fwd._presealed = [(body, dl_file)]

    fake_client = MagicMock()
    fake_client.ship_prepared = AsyncMock(return_value=None)

    # Force archive to fail
    fwd._archive_replayed = lambda sp, bid: False

    await fwd._flush_presealed(fake_client)

    # Ship succeeded once
    assert fake_client.ship_prepared.await_count == 1
    # But slot is retained because archive failed
    assert len(fwd._presealed) == 1
    # And the source file still exists (we didn't actually move it)
    assert dl_file.exists()


def test_schema_v1_to_v2_migration(tmp_path: Path):
    """A pre-existing DB from the v1 scaffold must upgrade in place without
    losing data. Simulates: install v0.1.0-pre, run it, upgrade code."""
    import sqlite3
    db_path = tmp_path / "state" / "offsets.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create a v1-era DB by hand (no last_line_number, no schema_meta).
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE file_offsets (
            file_path TEXT PRIMARY KEY,
            file_id TEXT,
            byte_offset INTEGER NOT NULL,
            last_sha256 TEXT,
            last_flushed_ns INTEGER NOT NULL
        );
        CREATE TABLE db_cursors (
            db_path TEXT PRIMARY KEY,
            table_name TEXT NOT NULL,
            last_row_id INTEGER NOT NULL,
            last_flushed_ns INTEGER NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO file_offsets VALUES (?, ?, ?, ?, ?)",
        ("/watched/a.jsonl", None, 12345, "deadbeef", 1_700_000_000_000_000_000),
    )
    conn.commit()
    conn.close()

    # Open via new StateDB — migration should add last_line_number without losing data.
    from logicd.state import StateDB
    st = StateDB(db_path)
    bo, ln = st.get_offset("/watched/a.jsonl")
    assert bo == 12345  # pre-existing byte offset preserved
    assert ln == 0      # newly-added column defaults to 0

    # Subsequent writes work with the new column.
    st.set_offset("/watched/a.jsonl", 99999, 42, "newhash", 1_800_000_000_000_000_000)
    bo, ln = st.get_offset("/watched/a.jsonl")
    assert bo == 99999
    assert ln == 42
    st.close()


def test_line_numbers_resume_from_saved_state(tmp_path: Path):
    src = tmp_path / "src" / "session-04.jsonl"
    _write_jsonl(src, [
        {"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}, {"n": 5},
    ])
    watches = (WatchEntry(name="t", adapter="claude_code", paths=(str(src),)),)
    cfg = _cfg(tmp_path, watches)
    fwd = Forwarder(cfg)

    # First drain: all 5 events, line numbers 1..5
    fwd._pending_files.add(str(src))
    fwd._drain_pending_files()
    assert [e["line_number"] for e in fwd.queue.events] == [1, 2, 3, 4, 5]

    # Simulate commit of only first 3 bytes boundary by advancing state directly
    first_three = fwd.queue.events[:3]
    fwd._commit_offsets(first_three)
    bo, ln = fwd.state.get_offset(str(src))
    assert ln == 3

    # Clear queue, re-tail: should resume from line 4
    fwd.queue.drain()
    fwd._pending_files.add(str(src))
    fwd._drain_pending_files()
    # File hasn't changed so we'd start from the saved offset, which is past line 3
    # But since we advanced only line_number to 3 without advancing byte_offset,
    # the next read starts at the saved byte_offset, continues counting from line 4.
    assert all(e["line_number"] >= 4 for e in fwd.queue.events)
