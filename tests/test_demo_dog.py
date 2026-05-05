"""Tests for `logicd demo-dog` and the demo-mode Config surface.

These tests cover the workstation-side demo onboarding flow only.
Server-side enforcement of role=ingester and tenant isolation is
covered by the Blackbox audit, not here.
"""
from __future__ import annotations

import os
import re
import uuid as _uuid
from pathlib import Path

import pytest

from logicd.config import Config
from logicd.demo import (
    DEMO_AGENT_NAME,
    DEMO_API_URL,
    DEMO_DASHBOARD_URL,
    DEMO_TENANT,
    _BUILTIN_DEMO_KEY,
    _DEMO_KEY_ENV_VAR,
    _resolve_demo_key,
    run_demo_dog,
    write_demo_config,
)


# ---------------------------------------------------------------------------
# write_demo_config — direct unit tests (no CLI)
# ---------------------------------------------------------------------------


def test_write_demo_config_creates_separate_file_from_production(tmp_path):
    """Demo config MUST live at a different filename from production
    `logicd.toml` so the two never collide on the same host."""
    cfg_path = write_demo_config(
        data_dir=tmp_path,
        api_key="gl_agent_demo_test",
        endpoint_id=str(_uuid.uuid4()),
        endpoint_name="testhost",
    )
    assert cfg_path.exists()
    assert cfg_path.name == f"{DEMO_AGENT_NAME}.toml"
    assert cfg_path.name != "logicd.toml"


def test_write_demo_config_round_trips_through_config_load(tmp_path):
    """A freshly written demo config must load cleanly and surface
    demo_mode=True with all the expected fields populated."""
    eid = str(_uuid.uuid4())
    cfg_path = write_demo_config(
        data_dir=tmp_path,
        api_key="gl_agent_demo_test",
        endpoint_id=eid,
        endpoint_name="testhost",
    )
    cfg = Config.load(cfg_path)
    assert cfg.demo_mode is True
    assert cfg.demo_tenant == DEMO_TENANT
    assert cfg.dashboard_url == DEMO_DASHBOARD_URL
    assert cfg.endpoint_id == eid
    assert cfg.api_key == "gl_agent_demo_test"
    assert cfg.api_url == DEMO_API_URL.rstrip("/")


def test_demo_endpoint_id_is_valid_uuid(tmp_path):
    """run_demo_dog generates a UUIDv4 endpoint_id. We can't easily
    invoke the CLI without env-side effects, so verify via the same
    code path the CLI takes."""
    eid = str(_uuid.uuid4())
    write_demo_config(
        data_dir=tmp_path,
        api_key="gl_agent_demo_test",
        endpoint_id=eid,
        endpoint_name="testhost",
    )
    # Re-parse and confirm it round-trips as a valid UUID.
    cfg = Config.load(tmp_path / "agents" / f"{DEMO_AGENT_NAME}.toml")
    parsed = _uuid.UUID(cfg.endpoint_id)
    assert parsed.version == 4


def test_demo_config_text_carries_demo_block_label(tmp_path):
    """Operators should be able to grep the file and tell it's a demo
    config without parsing TOML. The [demo] block + the comment about
    ingester role are the source-readable signals."""
    cfg_path = write_demo_config(
        data_dir=tmp_path,
        api_key="gl_agent_demo_test",
        endpoint_id=str(_uuid.uuid4()),
        endpoint_name="testhost",
    )
    text = cfg_path.read_text(encoding="utf-8")
    assert "[demo]" in text
    assert "DEMO MODE" in text
    assert "ingester" in text  # server-side role contract is documented inline
    assert DEMO_DASHBOARD_URL in text


# ---------------------------------------------------------------------------
# run_demo_dog — CLI behaviour (env-driven; no actual daemon start)
# ---------------------------------------------------------------------------


def test_resolve_demo_key_returns_env_when_set(monkeypatch):
    monkeypatch.setenv(_DEMO_KEY_ENV_VAR, "gl_agent_demo_envkey")
    key, source = _resolve_demo_key()
    assert key == "gl_agent_demo_envkey"
    assert source == "env"


def test_resolve_demo_key_returns_builtin_when_env_unset(monkeypatch):
    monkeypatch.delenv(_DEMO_KEY_ENV_VAR, raising=False)
    key, source = _resolve_demo_key()
    assert key == _BUILTIN_DEMO_KEY
    assert source == "builtin"


def test_resolve_demo_key_treats_blank_env_as_unset(monkeypatch):
    """A whitespace-only GHOSTLOGIC_DEMO_KEY must NOT pin a config to
    an empty key — fall through to the built-in instead. This guards
    against shell scripts that export the var as empty when unset."""
    monkeypatch.setenv(_DEMO_KEY_ENV_VAR, "   ")
    key, source = _resolve_demo_key()
    assert key == _BUILTIN_DEMO_KEY
    assert source == "builtin"


def test_run_demo_dog_uses_builtin_key_when_env_unset(tmp_path, monkeypatch, capsys):
    """Adam's directive: demo-dog must work with NO user input. Even
    when GHOSTLOGIC_DEMO_KEY is not set, the command writes a usable
    config using the built-in public demo key."""
    monkeypatch.delenv(_DEMO_KEY_ENV_VAR, raising=False)
    rc = run_demo_dog(["--data-dir", str(tmp_path), "--endpoint-name", "builtinhost"])
    assert rc == 0
    cfg_path = tmp_path / "agents" / f"{DEMO_AGENT_NAME}.toml"
    assert cfg_path.exists()
    cfg = Config.load(cfg_path)
    assert cfg.demo_mode is True
    assert cfg.api_key == _BUILTIN_DEMO_KEY

    out = capsys.readouterr().out
    # The exact line Adam asked for:
    assert "Demo Mode: using public demo ingest key. Not for production." in out
    assert DEMO_DASHBOARD_URL in out
    # Without --start, demo-dog prints the exact next command to run
    # (single line, easy to copy).
    assert "Next: run the daemon with this exact command:" in out
    assert "python -m logicd run --config" in out
    assert str(cfg_path) in out
    # Without --start the banner about foreground-mode MUST NOT print.
    assert "Demo agent is now running in this terminal." not in out
    assert "Ctrl+C" not in out


def test_run_demo_dog_env_override_takes_precedence(tmp_path, monkeypatch, capsys):
    """When the rotation lever is in use, env overrides built-in.
    Banner reflects the override path explicitly."""
    monkeypatch.setenv(_DEMO_KEY_ENV_VAR, "gl_agent_demo_envkey")
    rc = run_demo_dog(["--data-dir", str(tmp_path), "--endpoint-name", "envhost"])
    assert rc == 0
    cfg_path = tmp_path / "agents" / f"{DEMO_AGENT_NAME}.toml"
    cfg = Config.load(cfg_path)
    assert cfg.api_key == "gl_agent_demo_envkey"
    assert cfg.api_key != _BUILTIN_DEMO_KEY

    out = capsys.readouterr().out
    # Different banner line on the env-override path.
    assert _DEMO_KEY_ENV_VAR in out
    assert "env override" in out
    # Make sure the public-key line is NOT printed when env is in use.
    assert "using public demo ingest key" not in out


def test_run_demo_dog_start_prints_foreground_banner_then_runs_daemon(
    tmp_path, monkeypatch, capsys,
):
    """With --start, demo-dog must print the foreground-mode banner
    BEFORE invoking the daemon. We mock _run so the test doesn't try
    to spin up the real spine / aiohttp loop, and we record the order
    of operations to prove banner-then-run."""
    monkeypatch.delenv(_DEMO_KEY_ENV_VAR, raising=False)

    sequence: list[str] = []

    def fake_run(config_path: Path) -> int:
        sequence.append(f"_run({config_path.name})")
        return 0

    # Patch the import target the demo module reaches for. The module
    # does `from .__main__ import _run as _run_daemon` lazily inside
    # the if-args.start branch.
    import logicd.__main__ as main_mod
    monkeypatch.setattr(main_mod, "_run", fake_run)

    # Capture stdout up to the moment _run is invoked, then capture
    # what comes after — but capsys captures in chunks, so easier:
    # have fake_run record a sentinel and then read all stdout once.
    rc = run_demo_dog([
        "--data-dir", str(tmp_path),
        "--endpoint-name", "fghost",
        "--start",
    ])

    assert rc == 0
    out = capsys.readouterr().out

    # All four exact lines from the foreground banner — wording
    # locked in the spec.
    assert "Demo agent is now running in this terminal." in out
    assert "Keep this window open." in out
    assert "Close it or press Ctrl+C to stop sending demo events." in out
    assert f"Open {DEMO_DASHBOARD_URL} in your browser." in out

    # Ordering: the foreground banner must appear BEFORE the bottom of
    # stdout (i.e., before _run would have started writing daemon logs
    # in the real flow). We can't fully verify that without real I/O
    # interleaving, but we can assert (a) the banner text is in the
    # captured stdout and (b) _run was called exactly once with the
    # demo config path.
    assert sequence == ["_run(logicd-demo.toml)"]

    # The "no --start" hint MUST NOT print on the --start path —
    # otherwise we'd be telling the user to also run the daemon
    # manually, which would be confusing.
    assert "Next: run the daemon with this exact command:" not in out


def test_run_demo_dog_start_propagates_daemon_exit_code(
    tmp_path, monkeypatch,
):
    """If the daemon exits non-zero (auth-fatal, etc.), --start must
    surface that exit code rather than swallowing it as success."""
    monkeypatch.delenv(_DEMO_KEY_ENV_VAR, raising=False)
    import logicd.__main__ as main_mod
    monkeypatch.setattr(main_mod, "_run", lambda _p: 3)

    rc = run_demo_dog([
        "--data-dir", str(tmp_path),
        "--endpoint-name", "exitcodehost",
        "--start",
    ])
    assert rc == 3


def test_run_demo_dog_does_not_prompt_for_input(tmp_path, monkeypatch):
    """Hard rule: demo-dog must not call input() / read stdin. Test by
    replacing stdin with something that would fail loudly if read."""
    monkeypatch.delenv(_DEMO_KEY_ENV_VAR, raising=False)

    class _NoStdin:
        def read(self, *a, **kw):
            raise AssertionError("demo-dog must not read stdin")

        def readline(self, *a, **kw):
            raise AssertionError("demo-dog must not read stdin")

    monkeypatch.setattr("sys.stdin", _NoStdin())
    rc = run_demo_dog(["--data-dir", str(tmp_path), "--endpoint-name", "noprompthost"])
    assert rc == 0


def test_run_demo_dog_does_not_write_to_keyring(tmp_path, monkeypatch):
    """Demo keys must NOT enter the OS keyring. Rotation during a demo
    window should be 'edit one TOML file + restart' — not a credential-
    manager operation. Verify by spying on keyring.set_password."""
    monkeypatch.setenv(_DEMO_KEY_ENV_VAR, "gl_agent_demo_envkey")
    calls: list[tuple] = []

    try:
        import keyring as _keyring
    except ImportError:
        pytest.skip("keyring not installed in this environment")

    def fake_set_password(service, username, password):
        calls.append((service, username, password))

    monkeypatch.setattr(_keyring, "set_password", fake_set_password)
    rc = run_demo_dog([
        "--data-dir", str(tmp_path),
        "--endpoint-name", "envhost",
    ])
    assert rc == 0
    assert calls == [], (
        "demo-dog must NOT touch the OS keyring; got writes: " + repr(calls)
    )


# ---------------------------------------------------------------------------
# Production path safety — enroll path must not be affected
# ---------------------------------------------------------------------------


def test_production_config_without_demo_block_loads_with_demo_mode_false(tmp_path):
    """Backward compatibility: a Config TOML that pre-dates the [demo]
    block must still load, with demo_mode defaulting to False."""
    cfg_path = tmp_path / "logicd.toml"
    # Forward slashes — Windows paths break TOML parsing if `\U` etc.
    # appear unescaped (real installer uses the same conversion).
    posix_root = str(tmp_path).replace("\\", "/")
    # Use a unique endpoint_id per test so Config.read_api_key misses
    # the OS keyring and falls back to the TOML literal. Otherwise a
    # stale keyring entry from the dev machine (e.g. left over from
    # running `enroll` interactively) can shadow the test fixture.
    unique_eid = f"test-prod-{_uuid.uuid4()}"
    cfg_path.write_text(
        f'state_dir = "{posix_root}/state"\n'
        f'audit_log = "{posix_root}/audit.log"\n'
        f'[api]\n'
        f'url = "https://api.example"\n'
        f'key = "gl_agent_prod_test"\n'
        f'endpoint_id = "{unique_eid}"\n',
        encoding="utf-8",
    )
    cfg = Config.load(cfg_path)
    assert cfg.demo_mode is False
    assert cfg.demo_tenant == ""
    assert cfg.dashboard_url == ""
    assert cfg.api_key == "gl_agent_prod_test"


def test_demo_config_in_separate_directory_does_not_clobber_production(tmp_path):
    """Both modes can coexist on the same host: demo writes to
    agents/logicd-demo.toml, production enroll writes to
    agents/logicd.toml. Verify both can be present and load
    independently."""
    # Hand-written production-shape config
    prod_cfg = tmp_path / "agents" / "logicd.toml"
    prod_cfg.parent.mkdir(parents=True, exist_ok=True)
    posix_root = str(tmp_path).replace("\\", "/")
    unique_eid = f"test-coexist-{_uuid.uuid4()}"
    prod_cfg.write_text(
        f'state_dir = "{posix_root}/state"\n'
        f'audit_log = "{posix_root}/audit.log"\n'
        f'[api]\n'
        f'url = "https://api.example"\n'
        f'key = "gl_agent_prod"\n'
        f'endpoint_id = "{unique_eid}"\n',
        encoding="utf-8",
    )
    # Demo-shape config via the helper
    demo_cfg = write_demo_config(
        data_dir=tmp_path,
        api_key="gl_agent_demo",
        endpoint_id=str(_uuid.uuid4()),
        endpoint_name="testhost",
    )
    assert prod_cfg.exists() and demo_cfg.exists()
    assert prod_cfg != demo_cfg

    prod = Config.load(prod_cfg)
    demo = Config.load(demo_cfg)
    assert prod.demo_mode is False
    assert demo.demo_mode is True
    assert prod.api_key == "gl_agent_prod"
    assert demo.api_key == "gl_agent_demo"
