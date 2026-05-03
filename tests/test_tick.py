"""Tick arithmetic sanity."""
from logicd.tick import tick_window, tick_for_event_ns


def test_tick_index_stable_within_window():
    tick_s = 600
    tick_ns = tick_s * 1_000_000_000
    # Align base to a tick boundary so base + tick_ns - 1 stays in the same tick.
    base = (1_700_000_000_000_000_000 // tick_ns) * tick_ns
    t1 = tick_for_event_ns(base, tick_s)
    t2 = tick_for_event_ns(base + tick_ns - 1, tick_s)
    t3 = tick_for_event_ns(base + tick_ns, tick_s)
    assert t1 == t2
    assert t3 == t1 + 1


def test_tick_window_round_trip():
    tick_s = 600
    idx = 2_837_492
    start_ns, end_ns = tick_window(idx, tick_s)
    assert end_ns - start_ns == tick_s * 1_000_000_000 - 1
    assert tick_for_event_ns(start_ns, tick_s) == idx
    assert tick_for_event_ns(end_ns, tick_s) == idx
