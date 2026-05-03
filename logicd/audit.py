"""Append-only hash-chained audit log of forwarder activity.

Every line is JSON with:
- seq: monotonic sequence number
- prev_hash: sha256 of previous line (or "genesis" for seq=0)
- ts_ns: capture ns timestamp
- kind: event type (startup, batch_shipped, batch_failed, offset_update, ...)
- payload: arbitrary dict

The hash chain makes post-hoc tampering detectable: any edit breaks the chain.
"""
from __future__ import annotations
import json
import hashlib
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._prev_hash = self._recover()

    def _recover(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, "genesis"
        raw = self.path.read_bytes()
        # If the file doesn't end with a newline, the last write was truncated
        # mid-line. Cap it with one so the next emit() doesn't glue onto the
        # partial. Corrupted bytes stay on disk per the no-destroy rule; the
        # resulting unparsable line is skipped by the walker below and shows
        # up as a seq gap, which is itself a forensic signal.
        if not raw.endswith(b"\n"):
            with self.path.open("ab") as f:
                f.write(b"\n")
            raw = raw + b"\n"
        # Walk backwards from EOF to find the most recent line with a valid
        # seq. Truncated tails or corrupted records are skipped — never fatal.
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            seq = rec.get("seq")
            if isinstance(seq, int):
                return seq + 1, hashlib.sha256(line).hexdigest()
        return 0, "genesis"

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            rec = {
                "seq": self._seq,
                "prev_hash": self._prev_hash,
                "ts_ns": time.time_ns(),
                "kind": kind,
                "payload": payload,
            }
            line = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
            with self.path.open("ab") as f:
                f.write(line + b"\n")
            self._prev_hash = hashlib.sha256(line).hexdigest()
            self._seq += 1
