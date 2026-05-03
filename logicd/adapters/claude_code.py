"""Claude Code adapter. Reads `~/.claude/projects/<slug>/<session_uuid>.jsonl`."""
from __future__ import annotations
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import Adapter, make_event

SESSION_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _parse_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return None


class ClaudeCodeAdapter(Adapter):
    name = "claude_code"

    def tail_file(
        self,
        file_path: Path,
        start_offset: int,
        start_line_number: int,
        tick_seconds: int,
    ) -> Iterator[dict]:
        session_id = None
        m = SESSION_UUID_RE.search(file_path.stem)
        if m:
            session_id = m.group(1)
        project_slug = file_path.parent.name

        line_number = start_line_number
        with file_path.open("rb") as f:
            f.seek(start_offset)
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                offset = f.tell()
                # Incomplete line (no trailing newline yet) -- re-seek and bail until more is written
                if not raw.endswith(b"\n"):
                    f.seek(line_start)
                    break
                line_number += 1
                raw_stripped = raw.rstrip(b"\r\n")
                if not raw_stripped:
                    continue

                payload = None
                subtype = None
                event_ns = time.time_ns()
                try:
                    obj = json.loads(raw_stripped)
                    payload = obj
                    ts = obj.get("timestamp") or obj.get("ts") or obj.get("created_at")
                    parsed_ns = _parse_ts(ts) if isinstance(ts, str) else None
                    if parsed_ns is not None:
                        event_ns = parsed_ns
                    role = obj.get("role") or obj.get("type") or obj.get("message", {}).get("role")
                    if role:
                        subtype = f"role:{role}"
                except Exception:
                    payload = {"_parse_error": True, "raw": raw_stripped.decode("utf-8", errors="replace")}

                evt = make_event(
                    source="claude-code",
                    source_version=None,
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
                evt["project_slug"] = project_slug
                yield evt
