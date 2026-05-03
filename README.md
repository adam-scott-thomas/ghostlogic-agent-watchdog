# GhostLogic Agent Watchdog

GhostLogic Agent Watchdog is the product surface for the `logicd` collection daemon. It monitors local AI-agent coding sessions, including Codex CLI and Claude Code, seals them in rolling 10-minute ticks, and ships tamper-evident work receipts to GhostLogic Blackbox over HTTPS.

`logicd` remains the internal package, module, CLI, and service-runtime name. User-facing docs and plugin metadata should refer to GhostLogic Agent Watchdog.

Repository: `ghostlogic-agent-watchdog`. Plugin manifest name: `ghostlogic-agent-watchdog`. The daemon command remains `logicd`.

## Scope (v0.1.0)

- **Sources:** Claude Code (`~/.claude/projects/**/*.jsonl`), Codex CLI (`~/.codex/sessions/**/*.jsonl`, `~/.codex/history.jsonl`).
- **Capture model:** tick-based. 10-minute ticks. Every event carries its `tick_index`; the server-side aggregator applies a 7-day rolling window as retention policy. The client does not enforce retention.
- **Transport:** `POST https://api.ghostlogic.tech/api/v1/ingest` with `Authorization: Bearer <key>`.
- **Runtime:** foreground Python process or a platform-native service (systemd / launchd / NSSM, registered manually via copy-paste instructions printed by the installer; automatic service registration is TODO for v0.2.0). Read-only on source files. ACL-locked config.
- **Platforms:** Windows, macOS, Linux (all three).
- **Forensic posture:**
  - SHA-256 on every source line.
  - Deterministic `batch_id` (`sha256` of sorted event_ids) for idempotent retries and server dedupe.
  - Append-only hash-chained audit log (`audit.log`) of every forwarder activity.
  - Byte offsets advance only after a batch has been durably handled (shipped or dead-lettered). Process death before durability means the next run re-reads those bytes, so no data loss is expected.
  - Dead-lettered batches replay on startup with the original `batch_id` preserved.
  - Every event carries `line_number`, `byte_offset`, `byte_end`, `sha256`, source adapter, and `captured_at_ns` for pinpointable forensic mapping.

## Install

```bash
python -m logicd install
```

The installer prompts for your API key, writes a platform-native config, applies ACLs, and prints ready-to-paste service-registration instructions for your OS. The command stays `logicd`; the installed product is GhostLogic Agent Watchdog.

### Default locations

| Platform | Config + state directory | ACL method | Service instructions |
|---|---|---|---|
| Windows | `%PROGRAMDATA%\Logicd\` | `icacls` - SYSTEM + Administrators | NSSM or Task Scheduler |
| macOS | `~/Library/Application Support/Logicd/` | `chmod 600` - owner only | launchd LaunchAgent (per-user) |
| Linux | `$XDG_CONFIG_HOME/logicd/` or `~/.config/logicd/` | `chmod 600` - owner only | systemd --user unit (or system unit for root install) |

Override the default with `--data-dir`:

```bash
python -m logicd install --data-dir /opt/logicd
```

## Run (foreground)

```bash
python -m logicd run --config /path/to/config.toml
```

Paths work the same on all three OSes; use your platform's path form.

## Naming Split

- Product and plugin display name: `GhostLogic Agent Watchdog`
- Repository: `ghostlogic-agent-watchdog`
- Package and plugin manifest name: `ghostlogic-agent-watchdog`
- Internal Python package and CLI module: `logicd`
- Internal config, unit, and label identifiers may still use `logicd` where stability matters

## License

Apache-2.0

