"""Config loader. Reads TOML, never writes.

API-key resolution (audit F-WD-003, 2026-05-01):
  Priority order on `Config.load`:
    1. OS keyring entry under service `ghostlogic-agent-watchdog`,
       username `api_key:<endpoint_id>` (or `api_key:default` when no
       endpoint_id is configured yet).
    2. Plain `api.key` literal in the TOML file (legacy / fallback).
    3. KeyError if neither exists.

  The `enroll` and `install` commands write to BOTH locations so that
  freshly-enrolled agents work whether the keyring is reachable or not;
  steady-state operation prefers the keyring. To migrate an existing
  agent off plaintext TOML, run `python -m logicd migrate-key`.
"""
from __future__ import annotations
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import keyring as _keyring
except ImportError:  # graceful: fall through to TOML
    _keyring = None  # type: ignore[assignment]

KEYRING_SERVICE = "ghostlogic-agent-watchdog"


def _keyring_username(endpoint_id: str) -> str:
    return f"api_key:{endpoint_id or 'default'}"


def read_api_key(endpoint_id: str, toml_fallback: str) -> str:
    """Resolve api_key for the given endpoint_id. Prefers keyring."""
    if _keyring is not None:
        try:
            value = _keyring.get_password(KEYRING_SERVICE, _keyring_username(endpoint_id))
            if value:
                return value
        except Exception as e:  # keyring backend missing / locked
            logging.getLogger(__name__).warning(
                "Keyring read failed (%s); falling back to TOML literal", e
            )
    if toml_fallback:
        return toml_fallback
    raise RuntimeError(
        "No API key found in keyring or TOML for endpoint_id="
        f"{endpoint_id!r}. Run `logicd enroll` (or set api.key in the "
        "config file) before starting the daemon."
    )


def write_api_key(endpoint_id: str, api_key: str) -> bool:
    """Persist api_key to keyring. Returns True on success, False if
    the keyring backend is unavailable (caller should still write to
    TOML for compatibility in that case)."""
    if _keyring is None:
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, _keyring_username(endpoint_id), api_key)
        return True
    except Exception as e:
        logging.getLogger(__name__).warning("Keyring write failed: %s", e)
        return False


def delete_api_key(endpoint_id: str) -> bool:
    if _keyring is None:
        return False
    try:
        _keyring.delete_password(KEYRING_SERVICE, _keyring_username(endpoint_id))
        return True
    except Exception:
        return False


# Defaults are conservative: payloads stay on the machine unless the
# operator explicitly opts in. Redaction patterns cover the obvious
# secret shapes (provider tokens, JWTs, AWS keys, long base64 blobs).
_DEFAULT_REDACT_PATTERNS = (
    r"sk-[A-Za-z0-9_\-]{16,}",                      # OpenAI / Anthropic-style
    r"gl_agent_[A-Za-z0-9_\-]{16,}",                # GhostLogic agent keys
    r"gl_session_[A-Za-z0-9_\-]{16,}",              # GhostLogic session tokens
    r"gl_enroll_[A-Za-z0-9_\-]{16,}",               # GhostLogic enrollment tokens
    r"ghp_[A-Za-z0-9]{20,}",                        # GitHub PATs
    r"github_pat_[A-Za-z0-9_]{20,}",                # GitHub fine-grained
    r"AKIA[0-9A-Z]{16}",                            # AWS access key ID
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",  # JWT-shaped
    r"[A-Za-z0-9+/]{64,}={0,2}",                    # long base64 blobs
)


@dataclass(frozen=True)
class WatchEntry:
    name: str
    adapter: str
    paths: tuple[str, ...]
    sqlite: str | None = None


@dataclass(frozen=True)
class PrivacyConfig:
    """Controls what leaves the machine.

    include_payload=False (default): adapters strip the parsed JSON payload
      from every event before shipping. Server still receives event_id,
      sha256, byte offsets, line number, source, session id, hostname,
      timestamps — enough to prove the event existed without exposing the
      transcript content.

    redact_patterns: regex patterns. When include_payload=True, the
      payload's serialized form is scanned and matches replaced with
      `[REDACTED:<pattern_index>]`. Default list covers obvious secret
      shapes. Patterns can be added; defaults can be disabled with
      include_default_redactions=False.

    exclude_paths: glob patterns on the source file path. Events from
      files matching ANY pattern are dropped before they ever queue.
      Useful for blanket-excluding a project dir from capture."""

    include_payload: bool = False
    redact_patterns: tuple[str, ...] = ()
    include_default_redactions: bool = True
    exclude_paths: tuple[str, ...] = ()

    @property
    def effective_redact_patterns(self) -> tuple[str, ...]:
        if self.include_default_redactions:
            return _DEFAULT_REDACT_PATTERNS + self.redact_patterns
        return self.redact_patterns


@dataclass(frozen=True)
class Config:
    api_url: str
    api_key: str
    state_dir: Path
    audit_log: Path
    tick_seconds: int
    window_days: int
    max_concurrent_posts: int
    watches: tuple[WatchEntry, ...]
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    heartbeat_seconds: int = 60
    # endpoint_id: server-assigned UUID written to logicd.toml during
    # `logicd enroll`. Empty string for legacy configs that pre-date
    # endpoint isolation v1 (audit F-WD-002, 2026-05-01) — empty string
    # signals "fall back to hostname-only identity on the wire".
    endpoint_id: str = ""

    @staticmethod
    def load(path: str | Path) -> "Config":
        path = Path(path)
        with path.open("rb") as f:
            raw = tomllib.load(f)

        api = raw.get("api", {})
        tick = raw.get("tick", {})
        priv = raw.get("privacy", {})
        watches = tuple(
            WatchEntry(
                name=w["name"],
                adapter=w["adapter"],
                paths=tuple(w["paths"]),
                sqlite=w.get("sqlite"),
            )
            for w in raw.get("watch", [])
        )
        privacy = PrivacyConfig(
            include_payload=bool(priv.get("include_payload", False)),
            redact_patterns=tuple(priv.get("redact_patterns", ())),
            include_default_redactions=bool(priv.get("include_default_redactions", True)),
            exclude_paths=tuple(priv.get("exclude_paths", ())),
        )
        endpoint_id = str(api.get("endpoint_id", ""))
        # Prefer keyring; fall back to TOML literal. See module docstring.
        # `api.get("key", "")` because TOML may legitimately omit the
        # literal once migrated to keyring.
        resolved_api_key = read_api_key(endpoint_id, str(api.get("key", "")))
        return Config(
            api_url=api["url"].rstrip("/"),
            api_key=resolved_api_key,
            state_dir=Path(raw["state_dir"]).expanduser(),
            audit_log=Path(raw["audit_log"]).expanduser(),
            tick_seconds=int(tick.get("seconds", 600)),
            window_days=int(tick.get("window_days", 7)),
            max_concurrent_posts=int(api.get("max_concurrent_posts", 3)),
            watches=watches,
            privacy=privacy,
            heartbeat_seconds=int(raw.get("heartbeat_seconds", 60)),
            endpoint_id=endpoint_id,
        )
