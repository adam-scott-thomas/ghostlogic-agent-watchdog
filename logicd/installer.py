"""Cross-platform interactive installer for GhostLogic Agent Watchdog.

Honors the endpoint-isolation v1 layout: one GhostLogic root per machine,
multiple agents under it without colliding. Per platform:

- Windows: C:\\ProgramData\\GhostLogic\\ + icacls lockdown + NSSM/Task Scheduler
- macOS:   ~/Library/Application Support/GhostLogic/ + chmod 600 + launchd LaunchAgent
- Linux:   $XDG_CONFIG_HOME/ghostlogic/ (or ~/.config/ghostlogic/) + chmod 600 + systemd --user

Inside the root, this agent uses:
  agents/logicd.toml           # ACL-locked config
  state/logicd/                # offsets.db + dead-letter
  logs/logicd.log              # hash-chained audit log

Override the root via --data-dir; the substructure stays the same."""
from __future__ import annotations
import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


AGENT_NAME = "logicd"


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"  # also covers bsd/unix — conventions close enough


def default_data_dir() -> Path:
    """Platform-native default GhostLogic root (holds agents/, state/, logs/)."""
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "GhostLogic"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GhostLogic"
    # Linux / other Unix: XDG Base Directory spec
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ghostlogic"


def agent_paths(data_dir: Path, agent_name: str = AGENT_NAME) -> tuple[Path, Path, Path]:
    """Return (config_path, state_dir, audit_log) under the GhostLogic root."""
    return (
        data_dir / "agents" / f"{agent_name}.toml",
        data_dir / "state" / agent_name,
        data_dir / "logs" / f"{agent_name}.log",
    )


def prompt(prompt_text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt_text}{suffix}: ").strip()
    return raw or (default or "")


def write_config(*, data_dir: Path, api_key: str, agent_name: str = AGENT_NAME) -> Path:
    cfg_path, state_dir, audit_path = agent_paths(data_dir, agent_name)
    for d in (cfg_path.parent, state_dir, audit_path.parent):
        d.mkdir(parents=True, exist_ok=True)
    content = dedent(f"""
        # GhostLogic Agent Watchdog config for the internal logicd daemon.
        # Endpoint-isolation v1 layout: this file lives under
        # <ghostlogic_root>/agents/{agent_name}.toml. ACL-locked best-effort.

        state_dir = {str(state_dir).replace(chr(92), "/")!r}
        audit_log = {str(audit_path).replace(chr(92), "/")!r}

        [api]
        url = "https://api.ghostlogic.tech"
        key = "{api_key}"
        max_concurrent_posts = 3

        [tick]
        # 10-minute ticks. window_days is advisory; retention is enforced by
        # the server-side aggregator, which buckets events by tick_index.
        seconds = 600
        window_days = 7

        [[watch]]
        name = "claude-code"
        adapter = "claude_code"
        paths = ["~/.claude/projects/*/*.jsonl"]

        [[watch]]
        name = "codex-cli"
        adapter = "codex"
        paths = [
          "~/.codex/sessions/**/*.jsonl",
          "~/.codex/history.jsonl",
        ]
    """).strip() + "\n"
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def lock_acls(path: Path) -> tuple[bool, str]:
    """Restrict config to owner (or SYSTEM + Administrators on Windows).

    Returns (ok, description). `ok=False` means the lock step was skipped or
    failed -- installer continues but warns the user."""
    if sys.platform == "win32":
        # SYSTEM (for the daemon when run as a service), Administrators
        # (for ops), and the current user (the operator who ran enroll;
        # otherwise they can't even read their own config back). Read-only
        # for all three. /inheritance:r strips any other inherited ACEs.
        import getpass as _getpass
        me = _getpass.getuser()
        try:
            r = subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", "SYSTEM:(R)",
                 "/grant:r", "Administrators:(R)",
                 "/grant:r", f"{me}:(R)"],
                check=False, capture_output=True, text=True,
            )
            if r.returncode == 0:
                return True, f"icacls: SYSTEM + Administrators + {me} (read only)"
            return False, f"icacls failed: {r.stderr.strip()}"
        except FileNotFoundError:
            return False, "icacls not found"
    # Unix: chmod 600 (owner read/write only)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True, "chmod 600 (owner only)"
    except OSError as e:
        return False, f"chmod failed: {e}"


# ---------- service registration instructions (platform-native) -----------

def windows_service_instructions(python_exe: str, cfg_path: Path) -> str:
    # Service identifier MUST be space-free for ops convenience (sc.exe,
    # PowerShell tab-completion, log filenames). Display name carries the
    # human-readable brand.
    return dedent(f"""
        To run as a Windows service, pick one:

          NSSM (recommended):
            nssm install logicd "{python_exe}" -m logicd run --config "{cfg_path}"
            nssm set logicd DisplayName "GhostLogic Agent Watchdog"
            nssm set logicd Description "Forensic capture daemon for AI-agent coding sessions."
            nssm start logicd

          Task Scheduler (at system startup, runs as SYSTEM):
            schtasks /create /tn logicd /sc ONSTART /ru SYSTEM /rl HIGHEST ^
              /d "GhostLogic Agent Watchdog forensic capture daemon" ^
              /tr "\\"{python_exe}\\" -m logicd run --config \\"{cfg_path}\\""
            schtasks /run /tn logicd

        To run in foreground (testing):
          "{python_exe}" -m logicd run --config "{cfg_path}"
    """).strip()


def macos_service_instructions(python_exe: str, cfg_path: Path) -> str:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "tech.ghostlogic.logicd.plist"
    plist_body = dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key><string>tech.ghostlogic.logicd</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_exe}</string>
                <string>-m</string>
                <string>logicd</string>
                <string>run</string>
                <string>--config</string>
                <string>{cfg_path}</string>
            </array>
            <key>RunAtLoad</key><true/>
            <key>KeepAlive</key><true/>
            <key>StandardOutPath</key><string>/tmp/logicd.out.log</string>
            <key>StandardErrorPath</key><string>/tmp/logicd.err.log</string>
        </dict>
        </plist>
    """)
    return dedent(f"""
        To run as a macOS LaunchAgent (per-user):

          1. Save this to {plist_path}:

        {plist_body}
          2. Load it:
             launchctl load -w "{plist_path}"

          3. Verify:
             launchctl list | grep logicd

        To run in foreground (testing):
          "{python_exe}" -m logicd run --config "{cfg_path}"
    """).strip()


def linux_service_instructions(python_exe: str, cfg_path: Path) -> str:
    unit_path = Path.home() / ".config" / "systemd" / "user" / "logicd.service"
    unit_body = dedent(f"""\
        [Unit]
        Description=GhostLogic Agent Watchdog capture daemon (logicd)
        After=network.target

        [Service]
        Type=simple
        ExecStart={python_exe} -m logicd run --config {cfg_path}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
    """)
    return dedent(f"""
        To run as a systemd --user service (recommended, no sudo required):

          1. Save this to {unit_path}:

        {unit_body}
          2. Enable and start:
             systemctl --user daemon-reload
             systemctl --user enable --now logicd.service

          3. Watch logs:
             journalctl --user -u logicd.service -f

        For system-wide install (runs before login), put the unit under
        /etc/systemd/system/ and use `systemctl` without --user.

        To run in foreground (testing):
          "{python_exe}" -m logicd run --config "{cfg_path}"
    """).strip()


def service_instructions(python_exe: str, cfg_path: Path) -> str:
    plat = platform_name()
    if plat == "windows":
        return windows_service_instructions(python_exe, cfg_path)
    if plat == "macos":
        return macos_service_instructions(python_exe, cfg_path)
    return linux_service_instructions(python_exe, cfg_path)


# ---------- main ----------

def run_installer(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logicd install")
    ap.add_argument("--data-dir", help="Override the default data directory for config + state.")
    args = ap.parse_args(argv)

    plat = platform_name()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()

    print(f"GhostLogic Agent Watchdog installer  ({plat})")
    print("-" * 32)
    print(f"This installer will write a config at:  {data_dir / 'config.toml'}")
    print("...and restrict it to owner-only access via platform-native ACLs.")
    print("The internal daemon/module name remains `logicd` for runtime stability.")
    print("It will NOT auto-register a service. It prints platform-appropriate")
    print("service instructions you can paste. (Auto-service TODO v0.2.0.)")
    print()

    api_key = prompt("API key for api.ghostlogic.tech (gl_agent_...)", default=None)
    if not api_key.startswith("gl_agent_"):
        print("ERROR: key doesn't look like a gl_agent_ token.", file=sys.stderr)
        return 2

    cfg_path = write_config(data_dir=data_dir, api_key=api_key)
    ok, acl_desc = lock_acls(cfg_path)

    python_exe = sys.executable
    print()
    print(f"config written:  {cfg_path}")
    print(f"acl lock:        {acl_desc}" + ("" if ok else "  (WARNING: not locked)"))
    print()
    print(service_instructions(python_exe, cfg_path))
    print()
    return 0

