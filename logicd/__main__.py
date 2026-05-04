"""CLI entrypoint: `python -m logicd [install|run] ...`."""
from __future__ import annotations
import argparse
import asyncio
import signal
import sys
from pathlib import Path

from spine import Core

from .client import KeyRevokedOrUnauthorized
from .config import Config
from .enroll import run_enroll
from .installer import run_installer
from .watcher import Forwarder


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH_FATAL = 3  # 401/403 — bearer revoked, wrong scope, or unauthorized.


def main() -> int:
    parser = argparse.ArgumentParser(prog="logicd")
    sub = parser.add_subparsers(dest="cmd", required=True)

    install_p = sub.add_parser("install", help="Interactive cross-platform installer")
    install_p.add_argument("--data-dir", help="Override the default platform data directory")

    enroll_p = sub.add_parser(
        "enroll",
        help="Redeem a one-shot enrollment token, fetch a scoped agent key, write config",
    )
    enroll_p.add_argument("--token", required=True,
                          help="Enrollment token from the dashboard (gl_enroll_...)")
    enroll_p.add_argument("--api-url",
                          help="Override the API base URL (default: https://api.ghostlogic.tech)")
    enroll_p.add_argument("--data-dir", help="Override the default data directory")
    enroll_p.add_argument("--endpoint-name", help="Override the endpoint name")
    enroll_p.add_argument("--agent-id", default="logicd",
                          help="Agent identifier (default: logicd)")
    enroll_p.add_argument("--no-os-user", action="store_true",
                          help="Do not include OS user in enroll request")

    run_p = sub.add_parser("run", help="Run the forwarder in foreground")
    run_p.add_argument("--config", required=True, help="Path to config.toml")

    mig_p = sub.add_parser(
        "migrate-key",
        help="Move api.key from the TOML literal into the OS keyring "
             "(audit F-WD-003). Default is dry-run: pass --commit to "
             "actually blank out the TOML literal once you've verified "
             "the daemon still starts using the keyring entry.",
    )
    mig_p.add_argument("--config", required=True, help="Path to config.toml")
    mig_p.add_argument("--commit", action="store_true",
                       help="Actually blank out api.key in the TOML file "
                            "(default is dry-run that only writes to keyring)")

    args, unknown = parser.parse_known_args()
    if args.cmd == "install":
        extra = []
        if args.data_dir:
            extra = ["--data-dir", args.data_dir]
        return run_installer(extra + unknown)
    if args.cmd == "enroll":
        # Re-construct argv for run_enroll (it parses its own args).
        forwarded = ["--token", args.token, "--agent-id", args.agent_id]
        if args.api_url:
            forwarded += ["--api-url", args.api_url]
        if args.data_dir:
            forwarded += ["--data-dir", args.data_dir]
        if args.endpoint_name:
            forwarded += ["--endpoint-name", args.endpoint_name]
        if args.no_os_user:
            forwarded += ["--no-os-user"]
        return run_enroll(forwarded + unknown)
    if args.cmd == "run":
        return _run(Path(args.config))
    if args.cmd == "migrate-key":
        return _migrate_key(Path(args.config), commit=args.commit)
    return 1


def _migrate_key(config_path: Path, *, commit: bool) -> int:
    """Copy api.key from TOML to OS keyring. With --commit, blank the
    TOML literal so the next start sources from keyring only.

    Audit ref: F-WD-003 (2026-05-01)."""
    import tomllib as _tomllib
    from .config import write_api_key as _write_keyring

    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 1
    with config_path.open("rb") as f:
        raw = _tomllib.load(f)
    api = raw.get("api", {})
    toml_key = str(api.get("key", "") or "")
    endpoint_id = str(api.get("endpoint_id", "") or "")
    if not toml_key:
        print("Nothing to migrate: api.key in the TOML is already blank.")
        return 0

    ok = _write_keyring(endpoint_id, toml_key)
    if not ok:
        print("ERROR: keyring write failed. Refusing to blank the TOML "
              "literal — without keyring backing, the daemon would have "
              "no key to read.", file=sys.stderr)
        return 1
    print(f"Wrote key to OS keyring (service=ghostlogic-agent-watchdog, "
          f"username=api_key:{endpoint_id or 'default'}).")

    if not commit:
        print("Dry-run only. Re-run with --commit to blank the TOML literal.")
        return 0

    # Blank api.key in the TOML, preserving the rest of the file.
    text = config_path.read_text(encoding="utf-8")
    import re as _re
    new_text, n = _re.subn(
        r'^(\s*key\s*=\s*)"[^"\n]*"',
        r'\1""',
        text,
        count=1,
        flags=_re.MULTILINE,
    )
    if n == 0:
        print("WARN: could not find the api.key literal to blank in the "
              "TOML. The keyring write succeeded; manually clear the line "
              "if you want plaintext gone.", file=sys.stderr)
        return 0
    config_path.write_text(new_text, encoding="utf-8")
    print(f"Blanked api.key in {config_path}. Daemon will now read from "
          "the OS keyring.")
    return 0


def _run(config_path: Path) -> int:
    cfg = Config.load(config_path)
    spine = Core()
    spine.register("logicd.config_path", config_path.resolve())
    spine.register("logicd.config", cfg)
    spine.register("logicd.forwarder", Forwarder(cfg))
    spine.boot(env="ghostlogic-agent-watchdog")
    fwd = spine.get("logicd.forwarder")

    async def _main():
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, fwd.request_stop)
        await fwd.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except KeyRevokedOrUnauthorized as exc:
        print(
            f"[logicd] auth-fatal: HTTP {exc.status}. Daemon paused. "
            "Either the API key has been revoked, the body identity does "
            "not match the bearer's scope, or the server has not yet "
            "registered this endpoint. Mint a new key in the dashboard "
            "and restart the service.",
            file=sys.stderr,
        )
        return EXIT_AUTH_FATAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())


