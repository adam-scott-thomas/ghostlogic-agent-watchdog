"""Audit log hash chain integrity."""
import hashlib
import json
from pathlib import Path
from logicd.audit import AuditLog


def test_chain_is_hash_linked(tmp_path: Path):
    log_path = tmp_path / "audit.log"
    log = AuditLog(log_path)
    log.emit("startup", {"hostname": "test"})
    log.emit("batch_shipped", {"batch_id": "abc", "event_count": 5})
    log.emit("shutdown", {})

    lines = [line for line in log_path.read_bytes().splitlines() if line.strip()]
    assert len(lines) == 3
    prev = "genesis"
    for i, raw in enumerate(lines):
        rec = json.loads(raw)
        assert rec["seq"] == i
        assert rec["prev_hash"] == prev
        prev = hashlib.sha256(raw).hexdigest()


def test_recover_continues_chain(tmp_path: Path):
    log_path = tmp_path / "audit.log"
    log1 = AuditLog(log_path)
    log1.emit("startup", {})
    log1.emit("batch_shipped", {})
    # simulate restart: new AuditLog on same file should continue the chain
    log2 = AuditLog(log_path)
    log2.emit("shutdown", {})

    lines = [line for line in log_path.read_bytes().splitlines() if line.strip()]
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [r["seq"] for r in records] == [0, 1, 2]
    # hash chain continuous
    assert records[2]["prev_hash"] == hashlib.sha256(lines[1]).hexdigest()


def test_recover_skips_truncated_last_line(tmp_path: Path):
    """A crash mid-write leaves a partial JSON line at EOF.

    The recovery walker must skip it and fall back to the last *valid* record,
    not raise. Corrupt bytes stay on disk for forensic review."""
    log_path = tmp_path / "audit.log"
    log1 = AuditLog(log_path)
    log1.emit("startup", {})
    log1.emit("batch_shipped", {"batch_id": "abc"})
    # simulate truncated tail (e.g. SIGKILL between buffer flushes)
    with log_path.open("ab") as f:
        f.write(b'{"seq":2,"prev_hash":"deadbeef","ts_ns":17, partial')

    log2 = AuditLog(log_path)  # must not raise
    log2.emit("shutdown", {})

    records = [
        json.loads(line)
        for line in log_path.read_bytes().splitlines()
        if line.strip().startswith(b"{") and line.strip().endswith(b"}")
    ]
    # The two clean records survive plus the new shutdown. Partial line ignored.
    seqs = [r["seq"] for r in records]
    assert seqs == [0, 1, 2]
    # New shutdown chains from the last *valid* line (batch_shipped, seq=1).
    valid_lines = [
        line for line in log_path.read_bytes().splitlines()
        if line.strip().startswith(b"{") and line.strip().endswith(b"}")
    ]
    assert records[2]["prev_hash"] == hashlib.sha256(valid_lines[1]).hexdigest()


def test_recover_falls_back_to_genesis_on_total_corruption(tmp_path: Path):
    log_path = tmp_path / "audit.log"
    log_path.write_bytes(b"\x00\x00\x00 garbage \xff\xfe\nnot-json-either\n")
    log = AuditLog(log_path)  # must not raise
    log.emit("startup", {"after": "corruption"})
    # New entry restarts at seq=0 (genesis chain).
    new_records = [
        json.loads(line)
        for line in log_path.read_bytes().splitlines()
        if line.strip().startswith(b"{") and b'"seq"' in line
    ]
    assert new_records[-1]["seq"] == 0
    assert new_records[-1]["prev_hash"] == "genesis"
