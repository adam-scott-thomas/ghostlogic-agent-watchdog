"""`logicd enroll` writes a working config from the server's response."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from logicd.enroll import _write_enrolled_config, run_enroll


def test_write_enrolled_config_creates_v1_layout(tmp_path: Path):
    cfg_path = _write_enrolled_config(
        data_dir=tmp_path / "GhostLogic",
        agent_name="logicd",
        api_url="https://api.test.local",
        api_key="gl_agent_pretend_test_key",
        endpoint_id="ep-123",
    )
    import tomllib
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["api"]["url"] == "https://api.test.local"
    assert raw["api"]["key"] == "gl_agent_pretend_test_key"
    assert raw["api"]["endpoint_id"] == "ep-123"
    assert raw["privacy"]["include_payload"] is False
    assert "claude-code" in [w["name"] for w in raw["watch"]]
    assert "codex-cli" in [w["name"] for w in raw["watch"]]
    assert raw["heartbeat_seconds"] == 60


def test_run_enroll_rejects_malformed_token(capsys):
    rc = run_enroll([
        "--token", "not_an_enroll_token",
        "--api-url", "http://test.invalid",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "gl_enroll_" in err


def test_run_enroll_writes_config_on_success(tmp_path: Path, capsys):
    fake_response = {
        "plaintext_key": "gl_agent_xxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "key_id": "kid-1",
        "key_prefix": "gl_agent_xxx",
        "endpoint_id": "ep-1",
        "endpoint_name": "TestBox",
        "agent_id": "logicd",
        "allowed_event_types": ["tool_event", "heartbeat"],
        "api_url": "https://api.test.local",
    }

    def fake_post(url, body, **kwargs):
        assert url.endswith("/api/v1/agents/enroll")
        assert body["token"].startswith("gl_enroll_")
        assert body["agent_id"] == "logicd"
        assert "tool_event" in body["requested_event_types"]
        return 201, fake_response

    # Mock lock_acls — on Windows it locks out the test runner from reading
    # the config back; the standalone Unix test below verifies the real ACL.
    with patch("logicd.enroll._http_post", side_effect=fake_post), \
         patch("logicd.enroll.lock_acls",
               return_value=(True, "mocked: acl-lock-skipped-in-tests")):
        rc = run_enroll([
            "--token", "gl_enroll_pretend_long_string_for_test",
            "--api-url", "https://api.test.local",
            "--data-dir", str(tmp_path),
            "--endpoint-name", "TestBox",
        ])
    assert rc == 0

    cfg_path = tmp_path / "agents" / "logicd.toml"
    assert cfg_path.exists()
    import tomllib
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["api"]["key"] == fake_response["plaintext_key"]
    assert raw["api"]["endpoint_id"] == "ep-1"

    # Plaintext key is shown ONCE in stdout (the enrollment receipt). It's
    # also in the config file (locally, ACL-locked). Acceptable per spec.
    out = capsys.readouterr().out
    assert fake_response["plaintext_key"] in out
    assert "endpoint:" in out
    assert "agent_id:" in out


def test_run_enroll_returns_3_on_server_error(tmp_path: Path, capsys):
    def fake_post(url, body, **kwargs):
        return 401, {"detail": "token already used"}
    with patch("logicd.enroll._http_post", side_effect=fake_post):
        rc = run_enroll([
            "--token", "gl_enroll_some_used_token_xxxxxxx",
            "--api-url", "https://api.test.local",
            "--data-dir", str(tmp_path),
        ])
    assert rc == 3
    assert "token already used" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 600 is Unix-only")
def test_enrolled_config_is_acl_locked_unix(tmp_path: Path):
    fake_response = {
        "plaintext_key": "gl_agent_xxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "key_id": "k", "key_prefix": "gl_agent_xxx",
        "endpoint_id": "ep", "endpoint_name": "X", "agent_id": "logicd",
        "allowed_event_types": ["tool_event"],
        "api_url": "http://test",
    }
    with patch("logicd.enroll._http_post",
               side_effect=lambda *a, **k: (201, fake_response)):
        run_enroll([
            "--token", "gl_enroll_test_test_test_xxxxxxx",
            "--api-url", "http://test",
            "--data-dir", str(tmp_path),
        ])
    cfg_path = tmp_path / "agents" / "logicd.toml"
    mode = cfg_path.stat().st_mode & 0o777
    assert mode == 0o600
