"""Cross-platform installer helpers: default paths + service instructions."""
from __future__ import annotations
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from logicd.installer import (
    default_data_dir, service_instructions, platform_name,
    windows_service_instructions, macos_service_instructions,
    linux_service_instructions, write_config, lock_acls, agent_paths,
)


def test_platform_name_matches_sys_platform():
    assert platform_name() in ("windows", "macos", "linux")


def test_default_data_dir_windows():
    with patch.object(sys, "platform", "win32"):
        with patch.dict(os.environ, {"PROGRAMDATA": "C:/TestProgramData"}):
            d = default_data_dir()
            assert str(d).replace("\\", "/") == "C:/TestProgramData/GhostLogic"


def test_default_data_dir_macos():
    with patch.object(sys, "platform", "darwin"):
        d = default_data_dir()
        assert "Library/Application Support/GhostLogic" in str(d).replace("\\", "/")


def test_default_data_dir_linux_with_xdg():
    with patch.object(sys, "platform", "linux"):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/test_xdg"}, clear=False):
            d = default_data_dir()
            assert str(d).replace("\\", "/") == "/tmp/test_xdg/ghostlogic"


def test_default_data_dir_linux_without_xdg():
    with patch.object(sys, "platform", "linux"):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with patch.dict(os.environ, env, clear=True):
            d = default_data_dir()
            assert str(d).replace("\\", "/").endswith(".config/ghostlogic")


def test_agent_paths_match_endpoint_isolation_v1_layout(tmp_path: Path):
    cfg, state, audit = agent_paths(tmp_path / "GhostLogic")
    assert str(cfg).replace("\\", "/").endswith("GhostLogic/agents/logicd.toml")
    assert str(state).replace("\\", "/").endswith("GhostLogic/state/logicd")
    assert str(audit).replace("\\", "/").endswith("GhostLogic/logs/logicd.log")


def test_windows_instructions_contain_nssm_and_taskscheduler(tmp_path: Path):
    out = windows_service_instructions("C:/py/python.exe", tmp_path / "config.toml")
    # Service identifier is the space-free internal id; display name carries the brand.
    assert "nssm install logicd" in out
    assert 'nssm set logicd DisplayName "GhostLogic Agent Watchdog"' in out
    assert "schtasks /create /tn logicd" in out


def test_macos_instructions_contain_launchd(tmp_path: Path):
    out = macos_service_instructions("/usr/bin/python3", tmp_path / "config.toml")
    assert "launchctl load" in out
    assert "tech.ghostlogic.logicd" in out
    assert "RunAtLoad" in out  # the plist template


def test_linux_instructions_contain_systemd(tmp_path: Path):
    out = linux_service_instructions("/usr/bin/python3", tmp_path / "config.toml")
    assert "systemctl --user" in out
    assert "[Service]" in out
    assert "ExecStart=/usr/bin/python3 -m logicd run" in out


def test_service_instructions_dispatches_to_right_platform(tmp_path: Path):
    with patch.object(sys, "platform", "linux"):
        assert "systemctl" in service_instructions("py", tmp_path / "c.toml")
    with patch.object(sys, "platform", "darwin"):
        assert "launchctl" in service_instructions("py", tmp_path / "c.toml")
    with patch.object(sys, "platform", "win32"):
        assert "nssm" in service_instructions("py", tmp_path / "c.toml")


def test_write_config_creates_valid_toml(tmp_path: Path):
    import tomllib
    root = tmp_path / "GhostLogic"
    cfg_path = write_config(data_dir=root, api_key="gl_agent_test")
    assert cfg_path.exists()
    # Endpoint-isolation v1 layout under the root.
    assert cfg_path == root / "agents" / "logicd.toml"
    assert (root / "state" / "logicd").is_dir()
    assert (root / "logs").is_dir()
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["api"]["url"] == "https://api.ghostlogic.tech"
    assert raw["api"]["key"] == "gl_agent_test"
    assert raw["tick"]["seconds"] == 600
    # state_dir / audit_log point at the per-agent layout
    assert raw["state_dir"].endswith("state/logicd")
    assert raw["audit_log"].endswith("logs/logicd.log")
    # Two default watches: claude-code + codex-cli
    names = [w["name"] for w in raw["watch"]]
    assert "claude-code" in names and "codex-cli" in names


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 600 is Unix-only")
def test_lock_acls_sets_owner_only_on_unix(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text("test")
    ok, desc = lock_acls(p)
    assert ok
    assert "chmod 600" in desc
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600

