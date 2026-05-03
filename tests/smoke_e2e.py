"""End-to-end smoke test: server up → mint enroll token → CLI enroll →
config written → daemon's saved key works for /ingest → revoke → 403.

Runs entirely from a fresh venv install. Verifies the Definition of Done
in one Python invocation."""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _post(url: str, body: dict, headers: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, text


def _get(url: str, headers: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, text


def _wait_for(url: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    if len(sys.argv) < 6:
        print("usage: smoke_e2e.py <python_exe> <ghost_logic_dir> <smoke_dir> "
              "<port> <logicd_console_script>")
        return 2

    py = sys.argv[1]
    ghost_dir = sys.argv[2]
    smoke = Path(sys.argv[3]).resolve()
    port = int(sys.argv[4])
    logicd = sys.argv[5]

    smoke.mkdir(parents=True, exist_ok=True)
    db = smoke / "auth.db"
    if db.exists():
        db.unlink()

    env = {**os.environ,
           "PYTHONIOENCODING": "utf-8",
           "BLACKBOX_AUTH_DB": str(db),
           "BLACKBOX_DATA_DIR": str(smoke / "data"),
           "BLACKBOX_SCOPED_KEYS": "1",
           "BLACKBOX_API_KEYS": "",
           "BLACKBOX_PUBLIC_API_URL": f"http://127.0.0.1:{port}",
           "PYTHONPATH": os.pathsep.join([
               ghost_dir, str(Path(ghost_dir) / "Blackbox"),
           ]),
           }

    print(f"[smoke] starting server on :{port}")
    server = subprocess.Popen(
        [py, "-m", "uvicorn", "server_fastapi_v2:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for(f"http://127.0.0.1:{port}/health"):
            print("[smoke] FAIL: server didn't come up")
            return 1
        print("[smoke] server up")

        # 1) OAuth exchange → session token
        s, body = _post(f"http://127.0.0.1:{port}/api/v1/auth/oauth-exchange", {
            "provider": "google", "subject": "smoke",
            "email": "smoke@test.local", "display_name": "Smoke",
        })
        assert s == 200 and isinstance(body, dict), f"oauth: {s} {body}"
        sess = body["session_token"]
        print(f"[smoke] session: {sess[:18]}…")

        # 2) Mint enrollment token
        s, body = _post(f"http://127.0.0.1:{port}/api/v1/me/enrollment-tokens",
                        {"endpoint_name_hint": socket.gethostname()},
                        headers={"X-Session-Token": sess})
        assert s == 201, f"mint token: {s} {body}"
        enroll_token = body["token"]
        print(f"[smoke] enroll token: {enroll_token[:18]}…")

        # 3) Run logicd enroll (the CLI written to PATH by `pip install`)
        agent_dir = smoke / "agent_data"
        if agent_dir.exists():
            import shutil
            shutil.rmtree(agent_dir)
        rc = subprocess.run(
            [logicd, "enroll", "--token", enroll_token,
             "--api-url", f"http://127.0.0.1:{port}",
             "--data-dir", str(agent_dir),
             "--endpoint-name", socket.gethostname()],
            capture_output=True, text=True, encoding="utf-8",
        )
        if rc.returncode != 0:
            print("[smoke] FAIL: enroll exited", rc.returncode)
            print("STDOUT:", rc.stdout)
            print("STDERR:", rc.stderr)
            return 1
        print(f"[smoke] enroll OK (rc={rc.returncode})")

        # 4) Verify config + saved key work
        cfg = agent_dir / "agents" / "logicd.toml"
        assert cfg.exists(), f"config not written: {cfg}"
        import tomllib
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
        saved_key = raw["api"]["key"]
        assert saved_key.startswith("gl_agent_"), f"bad key: {saved_key[:20]}"
        print(f"[smoke] saved key: {saved_key[:18]}…  endpoint_id={raw['api']['endpoint_id']}")

        # 5) Ship a fake event with the saved key
        s, body = _post(f"http://127.0.0.1:{port}/api/v1/ingest", {
            "agent_id": "logicd", "endpoint_name": socket.gethostname(),
            "batch_id": "smoke_b1",
            "events": [{"event_id": "smoke_e1", "event_type": "tool_event",
                        "ts_ns": time.time_ns()}],
        }, headers={"Authorization": f"Bearer {saved_key}"})
        assert s == 200 and body.get("accepted") == 1, f"ingest: {s} {body}"
        print(f"[smoke] ingest accepted={body['accepted']}")

        # 6) Endpoint detail visible from dashboard
        s, eps = _get(f"http://127.0.0.1:{port}/api/v1/me/endpoints",
                       headers={"X-Session-Token": sess})
        assert s == 200, f"endpoints: {s} {eps}"
        assert len(eps["endpoints"]) == 1
        eid = eps["endpoints"][0]["endpoint_id"]
        print(f"[smoke] endpoint visible in dashboard: {eid}")

        # 7) Recent events visible per-endpoint
        s, evs = _get(f"http://127.0.0.1:{port}/api/v1/me/endpoints/{eid}/events",
                       headers={"X-Session-Token": sess})
        assert s == 200 and len(evs["events"]) >= 1, f"events: {s} {evs}"
        print(f"[smoke] events visible: {len(evs['events'])} recent")

        # 8) Revoke key → ingest now 401
        s, keys = _get(f"http://127.0.0.1:{port}/api/v1/me/keys",
                        headers={"X-Session-Token": sess})
        kid = keys["keys"][0]["key_id"]
        s, _ = _post(f"http://127.0.0.1:{port}/api/v1/me/keys/{kid}/revoke",
                      {}, headers={"X-Session-Token": sess})
        assert s == 200
        print(f"[smoke] key revoked")

        s, body = _post(f"http://127.0.0.1:{port}/api/v1/ingest", {
            "agent_id": "logicd", "endpoint_name": socket.gethostname(),
            "batch_id": "smoke_b2",
            "events": [{"event_id": "smoke_e2", "event_type": "tool_event"}],
        }, headers={"Authorization": f"Bearer {saved_key}"})
        assert s in (401, 403), f"post-revoke ingest should be 401/403: got {s} {body}"
        print(f"[smoke] post-revoke ingest correctly rejected with {s}")

        # 9) Audit chain integrity end-to-end
        s, av = _get(f"http://127.0.0.1:{port}/api/v1/me/audit/verify",
                      headers={"X-Session-Token": sess})
        assert s == 200 and av["valid"] is True, f"chain not valid: {av}"
        print(f"[smoke] audit chain valid; head_seq={av['head_seq']}")

        print("\n[smoke] PASS - definition of done satisfied")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
