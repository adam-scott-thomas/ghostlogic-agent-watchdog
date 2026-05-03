"""Tick arithmetic. Ticks bucket events into fixed-width windows of wall-clock time."""
from __future__ import annotations
import time


def current_tick(tick_seconds: int, now_ns: int | None = None) -> int:
    """Return the tick index for the given ns timestamp (or now)."""
    if now_ns is None:
        now_ns = time.time_ns()
    return now_ns // (tick_seconds * 1_000_000_000)


def tick_window(tick_index: int, tick_seconds: int) -> tuple[int, int]:
    """Return (start_ns, end_ns) for a given tick index."""
    start_ns = tick_index * tick_seconds * 1_000_000_000
    end_ns = start_ns + tick_seconds * 1_000_000_000 - 1
    return start_ns, end_ns


def tick_for_event_ns(event_ns: int, tick_seconds: int) -> int:
    """Bin an event's nanosecond timestamp into its tick index."""
    return event_ns // (tick_seconds * 1_000_000_000)
