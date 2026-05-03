"""Filesystem watcher + orchestrator. Wires adapters → queue → batcher → client.

Delivery invariant: an event's byte range in the source file is never marked
consumed (via set_offset) until the batch it belongs to has been durably
handled — either shipped to the server or written to the dead-letter queue.
If flush raises, events go back into the queue for retry and offsets do not
advance. This costs bandwidth on retry (server dedupes by deterministic
batch_id) but never loses data."""
from __future__ import annotations
import asyncio
import glob
import json
import time
from pathlib import Path
from typing import Iterable

from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from watchdog.observers import Observer

from fnmatch import fnmatch

from .adapters import ADAPTERS, Adapter
from .adapters.base import HOSTNAME
from .audit import AuditLog
from .batch import BatchQueue, next_flush_delay
from .client import IngestClient, KeyRevokedOrUnauthorized
from .config import Config, WatchEntry
from .redact import Redactor
from .state import StateDB


class _FileEventHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, notify):
        self._loop = loop
        self._notify = notify

    def _kick(self, path: str) -> None:
        self._loop.call_soon_threadsafe(self._notify, path)

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent) and not event.is_directory:
            self._kick(event.src_path)

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            self._kick(event.src_path)


class Forwarder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = StateDB(cfg.state_dir / "offsets.db")
        self.audit = AuditLog(cfg.audit_log)
        self.queue = BatchQueue()
        self._pending_files: set[str] = set()
        self._stop = asyncio.Event()
        self._adapters: dict[str, Adapter] = {}
        self._redactor = Redactor(cfg.privacy.effective_redact_patterns)
        # Pre-sealed dead-letter bodies awaiting re-ship. Each entry is
        # (body_dict, source_dl_file). Original batch_id is preserved so
        # server-side dedup works on replay.
        self._presealed: list[tuple[dict, Path]] = []

    def _adapter_for(self, watch: WatchEntry) -> Adapter:
        a = self._adapters.get(watch.adapter)
        if a is None:
            a = ADAPTERS[watch.adapter]()
            self._adapters[watch.adapter] = a
        return a

    def _expand(self, pattern: str) -> list[Path]:
        return [Path(p) for p in glob.glob(str(Path(pattern).expanduser()), recursive=True)]

    def _all_watched_paths(self) -> list[tuple[Path, WatchEntry]]:
        results: list[tuple[Path, WatchEntry]] = []
        for w in self.cfg.watches:
            for pat in w.paths:
                for p in self._expand(pat):
                    if p.is_file():
                        results.append((p, w))
        return results

    async def run(self) -> None:
        self.audit.emit("startup", {
            "hostname": HOSTNAME, "api_url": self.cfg.api_url,
            "tick_seconds": self.cfg.tick_seconds, "window_days": self.cfg.window_days,
            "watch_count": len(self.cfg.watches),
        })

        loop = asyncio.get_running_loop()
        observer = Observer()
        handler = _FileEventHandler(loop, self._on_file_event)

        for watch in self.cfg.watches:
            for pat in watch.paths:
                root = Path(pat).expanduser()
                # Schedule observer on the first existing ancestor of the pattern
                while not root.exists() and root.parent != root:
                    root = root.parent
                if root.is_dir():
                    observer.schedule(handler, str(root), recursive=True)
        observer.start()

        dead_letter = self.cfg.state_dir / "dead_letter"
        try:
            async with IngestClient(
                api_url=self.cfg.api_url,
                api_key=self.cfg.api_key,
                audit=self.audit,
                dead_letter_dir=dead_letter,
                max_concurrent=self.cfg.max_concurrent_posts,
            ) as client:
                self._replay_dead_letter(dead_letter)
                # Prime: pick up anything already on disk past the saved offset
                for p, w in self._all_watched_paths():
                    self._pending_files.add(str(p))
                try:
                    await self._orchestrate(client)
                except KeyRevokedOrUnauthorized as exc:
                    # Hard pause. The bearer is wrong, revoked, or doesn't
                    # match the body's identity. Silently dead-lettering
                    # would let the queue grow forever; instead, audit-emit
                    # and propagate so __main__.main returns a non-zero exit
                    # code and the service runtime surfaces the failure.
                    self.audit.emit("daemon_paused_revoked_key", {
                        "status": exc.status, "reason": str(exc),
                    })
                    raise
        finally:
            observer.stop()
            observer.join(timeout=5.0)
            self.state.close()
            self.audit.emit("shutdown", {})

    def _on_file_event(self, path: str) -> None:
        self._pending_files.add(path)

    async def _orchestrate(self, client: IngestClient) -> None:
        # Heartbeat at startup so the dashboard sees the device immediately.
        # heartbeat_seconds=0 disables heartbeats entirely (tests, debugging).
        if self.cfg.heartbeat_seconds > 0:
            self._enqueue_heartbeat("startup")
        last_heartbeat = time.time()
        while not self._stop.is_set():
            self._drain_pending_files()
            if (
                self.cfg.heartbeat_seconds > 0
                and time.time() - last_heartbeat >= self.cfg.heartbeat_seconds
            ):
                self._enqueue_heartbeat("periodic")
                last_heartbeat = time.time()
            if self.queue.should_flush_now:
                await self._flush(client)
            delay = next_flush_delay(self.queue)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            # Always attempt a flush on each tick. Both phases (presealed and
            # live) are no-ops when their respective work queues are empty, so
            # this is cheap. Gating on self.queue.count would strand presealed
            # dead-letter bodies indefinitely when no fresh events are arriving.
            await self._flush(client)

    def _enqueue_heartbeat(self, kind: str) -> None:
        """Synthesize a `heartbeat` event so the dashboard sees liveness even
        when no transcripts are being written."""
        from . import CAPTURED_BY, CAPTURE_VERSION, EVENT_SCHEMA_VERSION
        from .adapters.base import OS_USER
        from .tick import tick_for_event_ns
        import uuid as _uuid
        now_ns = time.time_ns()
        evt = {
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "event_type": "heartbeat",
            "event_id": str(_uuid.uuid4()),
            "subtype": kind,
            "tick_index": tick_for_event_ns(now_ns, self.cfg.tick_seconds),
            "captured_at_ns": now_ns,
            "captured_by": CAPTURED_BY,
            "capture_version": CAPTURE_VERSION,
            "hostname": HOSTNAME,
            "os_user": OS_USER,
            "source": "logicd",
            "source_version": CAPTURE_VERSION,
            "session_id": None,
            "file_path": "<heartbeat>",
            "line_number": 0,
            "byte_offset": 0,
            "byte_end": 0,
            "byte_length": 0,
            "hash_algorithm": "sha256",
            "sha256": "0" * 64,
            "ts_ns": now_ns,
            "payload": None,
        }
        self.queue.push(evt)

    def _drain_pending_files(self) -> None:
        """Tail each changed file and enqueue new events.

        Does NOT advance byte offsets here. Offsets move forward only after the
        events carrying those bytes have been durably handled by _flush()."""
        if not self._pending_files:
            return
        snapshot = list(self._pending_files)
        self._pending_files.clear()

        for raw_path in snapshot:
            p = Path(raw_path)
            if not p.exists() or not p.is_file():
                continue
            watch = self._watch_for_path(p, self.cfg.watches)
            if watch is None:
                continue
            if self._is_excluded(str(p)):
                continue
            adapter = self._adapter_for(watch)
            byte_offset, line_number = self.state.get_offset(str(p))
            for evt in adapter.tail_file(p, byte_offset, line_number, self.cfg.tick_seconds):
                self._apply_privacy(evt)
                self.queue.push(evt)

    def _is_excluded(self, path: str) -> bool:
        norm = path.replace("\\", "/")
        for pat in self.cfg.privacy.exclude_paths:
            if fnmatch(norm, pat):
                return True
        return False

    def _apply_privacy(self, evt: dict) -> None:
        """Strip or redact payload in place per the privacy config.

        Hashes / offsets / line numbers stay — they're proof-of-existence.
        Payload is the only field that can carry secrets."""
        if not self.cfg.privacy.include_payload:
            evt["payload"] = None
            evt["_payload_stripped"] = True
            return
        payload = evt.get("payload")
        if payload is not None:
            evt["payload"] = self._redactor.redact_payload(payload)

    @staticmethod
    def _watch_for_path(p: Path, watches: Iterable[WatchEntry]) -> WatchEntry | None:
        s = str(p).replace("\\", "/")
        for w in watches:
            for pat in w.paths:
                expanded = str(Path(pat).expanduser()).replace("\\", "/")
                # cheap prefix match on the directory root
                root = expanded.split("*")[0]
                if s.startswith(root):
                    return w
        return None

    async def _flush(self, client: IngestClient) -> None:
        """Two-phase flush:

        Phase 1 — replay any pre-sealed dead-letter bodies, ONE POST per body,
          preserving the original batch_id. Successful re-ship archives the DL
          source file to dead_letter/replayed/ (never deleted — Blackbeard).
          Failures leave the file in place for the next flush attempt.

        Phase 2 — ship fresh live events. Unchanged from prior behavior:
          offsets commit on success; exceptions re-queue events intact."""
        await self._flush_presealed(client)
        await self._flush_live(client)

    async def _flush_presealed(self, client: IngestClient) -> None:
        if not self._presealed:
            return
        remaining: list[tuple[dict, Path]] = []
        for body, src_path in self._presealed:
            bid = body.get("batch_id", "")
            try:
                await client.ship_prepared(body)
            except Exception as e:
                self.audit.emit("dead_letter_reship_failed", {
                    "batch_id": bid,
                    "source_file": str(src_path),
                    "error": f"{type(e).__name__}: {e}",
                })
                remaining.append((body, src_path))
                continue
            # Ship succeeded. Now try to archive. If the archive fails, we
            # KEEP the slot so the next flush can retry the archive. The
            # re-ship itself will be a server-side no-op on the same batch_id
            # (idempotent via deterministic id) — so retrying archive costs
            # one extra dedup hit per flush until it succeeds, no data dup.
            if not self._archive_replayed(src_path, bid):
                remaining.append((body, src_path))
        self._presealed = remaining

    def _archive_replayed(self, src_path: Path, bid: str) -> bool:
        """Move a successfully re-shipped DL file to dead_letter/replayed/.

        Returns True on success, False if archive failed. Caller keeps the
        in-memory slot on False so the next flush retries."""
        replayed_dir = src_path.parent / "replayed"
        try:
            replayed_dir.mkdir(exist_ok=True)
            dest = replayed_dir / src_path.name
            src_path.replace(dest)
            self.audit.emit("dead_letter_reshipped", {
                "batch_id": bid, "archived_to": str(dest),
            })
            return True
        except Exception as e:
            self.audit.emit("dead_letter_archive_failed", {
                "batch_id": bid, "source_file": str(src_path),
                "error": f"{type(e).__name__}: {e}",
            })
            return False

    async def _flush_live(self, client: IngestClient) -> None:
        events = self.queue.drain()
        if not events:
            return
        try:
            await client.ship(events, hostname=HOSTNAME)
            self._commit_offsets(events)
        except Exception as e:
            self.audit.emit("flush_failed", {
                "error": f"{type(e).__name__}: {e}",
                "event_count": len(events),
            })
            # Events are not durable. Re-queue so the next flush retries them.
            # Preserve order so line_number / offset invariants hold.
            self.queue.events = events + self.queue.events
            for ev in events:
                self.queue.bytes_accumulated += len(json.dumps(ev, separators=(",", ":")))

    def _commit_offsets(self, events: list[dict]) -> None:
        """Advance per-file offset + line_number to the highest durable event."""
        per_file: dict[str, tuple[int, int, str]] = {}
        for ev in events:
            path = ev["file_path"]
            be = ev["byte_end"]
            ln = ev["line_number"]
            sha = ev["sha256"]
            current = per_file.get(path)
            if current is None or be > current[0]:
                per_file[path] = (be, ln, sha)
        now = time.time_ns()
        for path, (be, ln, sha) in per_file.items():
            self.state.set_offset(path, be, ln, sha, now)

    def _replay_dead_letter(self, dead_letter_dir: Path) -> None:
        """On startup, load dead-lettered batches as pre-sealed bodies.

        Each DL file becomes exactly ONE pre-sealed (body, source_path) entry.
        The original batch_id is preserved; no merging with live events; no
        re-hashing. Server-side dedup works because the batch_id we POST on
        replay is the same one the server may have already partially seen.

        Files already moved to dead_letter/replayed/ by a prior successful
        re-ship are NOT picked up again (they're in a sibling directory that
        this glob doesn't touch)."""
        if not dead_letter_dir.exists():
            return
        loaded = 0
        for f in sorted(dead_letter_dir.glob("*.json")):
            if f.parent.name == "replayed":
                continue  # defensive; glob above shouldn't hit it
            try:
                data = json.loads(f.read_bytes())
                body = data.get("body", {})
                if not isinstance(body.get("events"), list) or not body.get("batch_id"):
                    self.audit.emit("dead_letter_replay_error", {
                        "file": str(f), "error": "malformed body (missing events or batch_id)",
                    })
                    continue
                self._presealed.append((body, f))
                loaded += 1
            except Exception as e:
                self.audit.emit("dead_letter_replay_error", {
                    "file": str(f), "error": f"{type(e).__name__}: {e}",
                })
        if loaded:
            self.audit.emit("dead_letter_loaded_for_replay", {
                "file_count": loaded,
                "total_events": sum(len(b.get("events", [])) for b, _ in self._presealed),
            })

    def request_stop(self) -> None:
        self._stop.set()
