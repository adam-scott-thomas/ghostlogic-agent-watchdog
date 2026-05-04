"""HTTP client for POST /api/v1/ingest. Retries with exponential backoff.

Batches that exhaust retries are persisted to a dead-letter dir so nothing
is ever dropped silently. Operator can replay later."""
from __future__ import annotations
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from . import CAPTURED_BY, __version__
from .audit import AuditLog
from .batch import batch_id


DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30.0, connect=10.0)


class IngestClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        audit: AuditLog,
        dead_letter_dir: Path,
        max_concurrent: int = 3,
        endpoint_id: str = "",
    ):
        self.api_url = api_url
        self.endpoint_id = endpoint_id  # F-WD-002: surface endpoint_id on the wire
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"{CAPTURED_BY}/{__version__}",
        }
        self.audit = audit
        self.dead_letter_dir = dead_letter_dir
        self.dead_letter_dir.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(max_concurrent)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "IngestClient":
        self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    async def ship(self, events: list[dict], *, hostname: str) -> None:
        """Ship fresh live events. Builds a new batch envelope with a batch_id
        derived from the events. On retry exhaustion, parks the full body to
        the dead-letter queue (preserving that same batch_id) for later replay."""
        if not events:
            return
        bid = batch_id(events)
        body = {
            "source_id": hostname,
            "agent_id": CAPTURED_BY,
            "endpoint_name": hostname,
            "batch_id": bid,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "events": events,
        }
        # F-WD-002 (2026-05-01): emit endpoint_id when configured so the
        # server can correlate batches by endpoint UUID instead of just
        # hostname (which collides across virtualised endpoints).
        if self.endpoint_id:
            body["endpoint_id"] = self.endpoint_id

        async with self._sem:
            await self._post_with_retry(body, park_on_exhaustion=True)

    async def ship_prepared(self, body: dict) -> None:
        """Ship a fully-formed batch envelope whose batch_id MUST be preserved.

        Used by dead-letter replay: the caller owns the original DL file and is
        responsible for moving it to an archive directory on success OR leaving
        it in place on failure. We therefore do NOT re-park on exhaustion here —
        we raise instead, so the caller can keep retrying the same file next
        flush without creating duplicate DL entries under new batch_ids."""
        async with self._sem:
            await self._post_with_retry(body, park_on_exhaustion=False)

    async def _post_with_retry(self, body: dict, *, park_on_exhaustion: bool) -> None:
        assert self._session is not None
        bid = body["batch_id"]
        events = body.get("events", [])
        url = f"{self.api_url}/api/v1/ingest"
        # Idempotency-Key matches batch_id so server-side dedup is explicit and
        # crash-restart retries (which produce the same deterministic batch_id)
        # are guaranteed safe even if the server-side dedup window is short.
        headers = {**self._headers, "Idempotency-Key": bid}
        backoff = 1.0
        for attempt in range(6):
            try:
                async with self._session.post(url, json=body, headers=headers) as r:
                    status = r.status
                    text = await r.text()
                    if 200 <= status < 300:
                        self.audit.emit("batch_shipped", {
                            "batch_id": bid, "event_count": len(events),
                            "status": status, "attempt": attempt,
                        })
                        return
                    if status in (429,) or 500 <= status < 600:
                        self.audit.emit("batch_retry", {
                            "batch_id": bid, "status": status,
                            "attempt": attempt, "response_head": text[:500],
                        })
                        await asyncio.sleep(backoff + random.uniform(0, 0.5))
                        backoff = min(backoff * 2, 60.0)
                        continue
                    # 401/403 are auth-fatal: the key is wrong, revoked, or
                    # the body's identity doesn't match the bearer's scope.
                    # Dead-lettering on 401/403 silently buries events while
                    # the queue grows. Raise instead so the orchestrator can
                    # hard-pause the daemon and surface the failure.
                    if status in (401, 403):
                        self.audit.emit("key_revoked_or_unauthorized", {
                            "batch_id": bid, "status": status,
                            "response_head": text[:500],
                        })
                        raise KeyRevokedOrUnauthorized(status, text[:500])
                    # Other 4xx → permanent reject (e.g. 422 schema violation)
                    self.audit.emit("batch_rejected", {
                        "batch_id": bid, "status": status,
                        "response_head": text[:500],
                    })
                    if park_on_exhaustion:
                        self._park_dead_letter(body, bid, reason=f"http_{status}")
                        return
                    raise ShipPermanentReject(status, text[:500])
            except aiohttp.ClientError as e:
                self.audit.emit("batch_client_error", {
                    "batch_id": bid, "attempt": attempt,
                    "error": f"{type(e).__name__}: {e}",
                })
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 60.0)

        if park_on_exhaustion:
            self._park_dead_letter(body, bid, reason="retries_exhausted")
        else:
            raise ShipRetriesExhausted(bid)

    def _park_dead_letter(self, body: dict, bid: str, *, reason: str) -> None:
        dest = self.dead_letter_dir / f"{int(time.time_ns())}_{bid[:12]}.json"
        with dest.open("wb") as f:
            f.write(json.dumps({"reason": reason, "body": body}).encode())
        self.audit.emit("batch_dead_lettered", {"batch_id": bid, "reason": reason, "path": str(dest)})


class ShipPermanentReject(Exception):
    def __init__(self, status: int, body_head: str):
        super().__init__(f"HTTP {status}: {body_head}")
        self.status = status


class ShipRetriesExhausted(Exception):
    def __init__(self, batch_id: str):
        super().__init__(f"retries exhausted for batch {batch_id}")
        self.batch_id = batch_id


class KeyRevokedOrUnauthorized(Exception):
    """Server returned 401 or 403. Daemon must pause — silently parking
    these to the dead-letter queue would let the queue grow forever while
    the operator wonders why no events are landing. The orchestrator
    catches this and exits with a non-zero code so the service runtime
    surfaces the failure (and doesn't auto-restart into the same state)."""

    def __init__(self, status: int, body_head: str):
        super().__init__(f"HTTP {status} (key revoked / unauthorized): {body_head}")
        self.status = status
