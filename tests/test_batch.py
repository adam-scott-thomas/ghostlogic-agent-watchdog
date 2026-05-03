"""Batch determinism + cadence rules."""
from logicd.batch import BatchQueue, batch_id


def _ev(i: int) -> dict:
    return {"event_id": f"e{i:04d}", "payload": {"k": "v"}}


def test_batch_id_is_order_independent():
    a = [_ev(3), _ev(1), _ev(2)]
    b = [_ev(1), _ev(2), _ev(3)]
    assert batch_id(a) == batch_id(b)


def test_queue_thresholds():
    q = BatchQueue()
    assert not q.is_loaded
    for i in range(200):
        q.push(_ev(i))
    assert q.is_loaded
    drained = q.drain()
    assert len(drained) == 200
    assert q.count == 0
    assert not q.is_loaded
