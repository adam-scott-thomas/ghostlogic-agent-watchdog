"""Adapter contract + shared event construction."""
from __future__ import annotations
import hashlib
import socket
import getpass
import uuid
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from .. import CAPTURE_VERSION, CAPTURED_BY, EVENT_SCHEMA_VERSION
from ..tick import tick_for_event_ns


HOSTNAME = socket.gethostname()
OS_USER = getpass.getuser()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_event(
    *,
    source: str,
    source_version: str | None,
    session_id: str | None,
    file_path: str,
    line_number: int,
    byte_offset: int,
    byte_end: int,
    raw_line_bytes: bytes,
    event_ns: int,
    tick_seconds: int,
    payload: dict | None,
    subtype: str | None = None,
) -> dict:
    """Canonical event shape. Every adapter MUST produce events through this.

    byte_offset + byte_length = bytes of the payload; byte_end is the file offset
    just after the terminating newline (what file.tell() returns after readline())."""
    return {
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event_type": "tool_event",
        "event_id": str(uuid.uuid4()),
        "subtype": subtype,
        "tick_index": tick_for_event_ns(event_ns, tick_seconds),
        "captured_at_ns": time.time_ns(),
        "captured_by": CAPTURED_BY,
        "capture_version": CAPTURE_VERSION,
        "hostname": HOSTNAME,
        "os_user": OS_USER,
        "source": source,
        "source_version": source_version,
        "session_id": session_id,
        "file_path": file_path,
        "line_number": line_number,
        "byte_offset": byte_offset,
        "byte_end": byte_end,
        "byte_length": len(raw_line_bytes),
        "hash_algorithm": "sha256",
        "sha256": _sha256(raw_line_bytes),
        "ts_ns": event_ns,
        "payload": payload,
    }


class Adapter(ABC):
    """Adapters read from their source and yield canonical events."""

    name: str

    @abstractmethod
    def tail_file(
        self,
        file_path: Path,
        start_offset: int,
        start_line_number: int,
        tick_seconds: int,
    ) -> Iterator[dict]:
        """Yield canonical events for each line past start_offset.

        Events carry their own byte_end and line_number so the forwarder can
        commit offsets only after a batch is durably handled. Adapters MUST
        open the file read-only and MUST NOT mutate the source in any way."""
        ...
