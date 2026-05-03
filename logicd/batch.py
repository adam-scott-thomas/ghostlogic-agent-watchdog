"""Batching + flushing. Tick-boundary-triggered OR size/count-triggered.

A batch ships all events accumulated in a tick window as one POST. The batch_id
is sha256 of sorted event_ids so retries are idempotent server-side."""
from __future__ import annotations
import hashlib
import json
import random
from dataclasses import dataclass, field

MAX_EVENTS_PER_BATCH = 1000   # well under server's 10k limit
MAX_BYTES_PER_BATCH = 5 * 1024 * 1024
IDLE_FLUSH_SECONDS = 5.0
LOADED_FLUSH_SECONDS = 1.0
LOADED_JITTER_SECONDS = 0.05
LOADED_THRESHOLD_EVENTS = 200
LOADED_THRESHOLD_BYTES = 1 * 1024 * 1024


def batch_id(events: list[dict]) -> str:
    ids = sorted(e["event_id"] for e in events)
    h = hashlib.sha256()
    for eid in ids:
        h.update(eid.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class BatchQueue:
    events: list[dict] = field(default_factory=list)
    bytes_accumulated: int = 0

    def push(self, event: dict) -> None:
        self.events.append(event)
        self.bytes_accumulated += len(json.dumps(event, separators=(",", ":")))

    def drain(self) -> list[dict]:
        out, self.events = self.events, []
        self.bytes_accumulated = 0
        return out

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def is_loaded(self) -> bool:
        return (
            self.count >= LOADED_THRESHOLD_EVENTS
            or self.bytes_accumulated >= LOADED_THRESHOLD_BYTES
        )

    @property
    def should_flush_now(self) -> bool:
        return (
            self.count >= MAX_EVENTS_PER_BATCH
            or self.bytes_accumulated >= MAX_BYTES_PER_BATCH
        )


def next_flush_delay(queue: BatchQueue) -> float:
    """Adaptive flush cadence: 5s idle, 1s ± 50ms jitter under load."""
    if queue.is_loaded:
        return LOADED_FLUSH_SECONDS + random.uniform(-LOADED_JITTER_SECONDS, LOADED_JITTER_SECONDS)
    return IDLE_FLUSH_SECONDS
