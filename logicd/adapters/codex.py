"""Codex adapter. Reads `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and history.jsonl."""
from __future__ import annotations
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import Adapter, make_event

ROLLOUT_RE = re.compile(r"rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)


def _parse_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return None


class CodexAdapter(Adapter):
    name = "codex"

    def tail_file(
        self,
        file_path: Path,
        start_offset: int,
        start_line_number: int,
        tick_seconds: int,
    ) -> Iterator[dict]:
        session_id = None
        is_rollout = False
        m = ROLLOUT_RE.search(file_path.name)
        if m:
            session_id = m.group(1)
            is_rollout = True

        line_number = start_line_number
        with file_path.open("rb") as f:
            f.seek(start_offset)
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                offset = f.tell()
                if not raw.endswith(b"\n"):
                    f.seek(line_start)
                    break
                line_number += 1
                raw_stripped = raw.rstrip(b"\r\n")
                if not raw_stripped:
                    continue

                payload = None
                subtype = None
                source_version = None
                event_ns = time.time_ns()
                try:
                    obj = json.loads(raw_stripped)
                    payload = obj

                    if is_rollout:
                        # rollout line shape: {"timestamp", "type", "payload"}
                        ts = obj.get("timestamp")
                        parsed = _parse_ts(ts) if isinstance(ts, str) else None
                        if parsed is not None:
                            event_ns = parsed
                        subtype = obj.get("type")
                        if subtype == "session_meta":
                            cli_version = obj.get("payload", {}).get("cli_version")
                            if cli_version:
                                source_version = f"codex-tui/{cli_version}"
                            if not session_id:
                                session_id = obj.get("payload", {}).get("id")
                    else:
                        # history.jsonl shape: {"session_id", "ts", "text"}
                        if isinstance(obj.get("ts"), int):
                            event_ns = int(obj["ts"]) * 1_000_000_000
                        if not session_id:
                            session_id = obj.get("session_id")
                        subtype = "history_entry"
                except Exception:
                    payload = {"_parse_error": True, "raw": raw_stripped.decode("utf-8", errors="replace")}

                evt = make_event(
                    source="codex-cli",
                    source_version=source_version,
                    session_id=session_id,
                    file_path=str(file_path).replace("\\", "/"),
                    line_number=line_number,
                    byte_offset=line_start,
                    byte_end=offset,
                    raw_line_bytes=raw_stripped,
                    event_ns=event_ns,
                    tick_seconds=tick_seconds,
                    payload=payload,
                    subtype=subtype,
                )
                yield evt
