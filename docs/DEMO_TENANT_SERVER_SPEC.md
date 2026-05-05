# Demo tenant server-side spec — operator handoff

This document is the contract between the workstation `logicd demo-dog` onboarding flow (already shipped 2026-05-05) and the production Blackbox server + dashboard. The agent-side code assumes the items in this document are in place server-side. They are not yet.

Audience: the VM operator + whoever owns the dashboard repo. Read all four sections before provisioning anything live.

---

## 1. Mint the demo ingester key

**Where:** `/etc/blackbox/keys.json` on the production VM (`api.ghostlogic.tech`). Hot-reloaded by `_load_key_registry()` on the running uvicorn.

**Required entry (object-keyed schema — adapt to whatever schema your live file uses):**

```json
{
  "<DEMO_KEY_VALUE>": {
    "name": "ghostlogic-demo-ingester",
    "tenant": "ghostlogic-demo",
    "role": "ingester",
    "scopes": ["ingest"],
    "created_at": "2026-05-05T00:00:00Z",
    "demo": true
  }
}
```

Constraints — these are load-bearing for the security envelope:

- **`role: "ingester"`** — the only thing this key is allowed to do is `POST /api/v1/ingest` and `POST /api/v1/heartbeat` (or whatever the equivalents are in production v2).
- **`role` MUST NOT be `tenant`, `admin`, or `reader`.** Specifically: this key MUST be rejected by `/api/v1/verify`, `/api/v1/capsules`, `/api/v1/capsules/{id}`, `/api/v1/capsules/{id}/download`, `/api/v1/buffer/recent`, `/api/v1/me`. If your auth implementation doesn't already gate by role, add the check before this key goes live.
- **`tenant: "ghostlogic-demo"`** — separate tenant_id from any real customer's. Capsule storage namespacing should follow whatever you use for tenant isolation today.
- **`demo: true`** flag is workstation-side metadata for log/audit clarity; the server can ignore it but please preserve it on round-trip.

Generate the key value with whatever shape your live keys use (matching prefix + length so log redaction patterns still work). The workstation places the value into the agent config, not the keyring.

**Provisioning the key into the agent install:**

```bash
# At install / build time on the agent host:
export GHOSTLOGIC_DEMO_KEY="<DEMO_KEY_VALUE>"
python -m logicd demo-dog
```

The `demo-dog` command reads `GHOSTLOGIC_DEMO_KEY` from env and bakes it into `<data_dir>/agents/logicd-demo.toml`. If unset, it errors and refuses to write a config (refuses the placeholder). For a packaged release, substitute the placeholder at build time via sed/rewrite.

---

## 2. Tenant isolation invariants

The demo tenant `ghostlogic-demo` MUST be isolated from real tenants. Concretely:

- **Capsule storage**: demo capsules should land under a separate path / prefix from production tenants (e.g. `/opt/blackbox/data/capsules/ghostlogic-demo/...`). If your storage layout is `<root>/capsules/<tenant_id>/<capsule_id>.glcf.gz` you already get this for free; just confirm it.
- **Cross-tenant queries forbidden**: any read endpoint (`/api/v1/capsules`, `/api/v1/buffer/recent`, etc.) called with the demo key MUST 401/403, not serve real-tenant data. This follows from the role=ingester gate above; mention it because it's the high-risk failure mode if the role check is missing.
- **Quotas**: not load-bearing for this ship, but worth a thought — set a low quota on the demo tenant (e.g. 10k events/day) so a runaway demo install can't fill the disk. Defer to your existing quota infra if any.

---

## 3. Dashboard `/demo` route

**Route:** `https://blackbox.ghostlogic.tech/demo` (the dashboard host, NOT the API host).

**Hard requirements:**

- **The demo ingester key MUST NOT be embedded in the client.** Browsers should never see it.
- **No user-supplied API key for demo mode.** A visitor lands on `/demo` and sees data. They don't paste anything.
- **Read path uses one of:**
  - **(Recommended) Backend proxy.** The Next.js server (or whatever the dashboard runtime is) holds a separate **read-only key** for `tenant: ghostlogic-demo` in server-side env. `/demo`'s `getServerSideProps` / route handlers proxy filtered queries to `api.ghostlogic.tech` with that key. Client-side JS never touches a key.
  - **Fallback: scoped read-only token issued per session.** The dashboard mints a short-lived session token bound to `tenant: ghostlogic-demo` + read-only role + IP, hands it to the client, and the API enforces TTL. Higher engineering cost; only do this if the proxy approach doesn't fit your stack.
- **`Demo Mode` visual label.** Persistent banner (not a toast) at the top of every `/demo` page, with the tenant name + a link out to the production sign-up page. This is per the workstation onboarding banner; users who shipped from the agent should see the same framing on the dashboard.
- **No admin keys.** Dashboard should have zero admin endpoints reachable from `/demo`. List, revoke, mint — none of those should be hit from the demo route, even server-side.

**Provisioning the dashboard's read-only demo key:**

Mint a separate key in `/etc/blackbox/keys.json` for the dashboard, like:

```json
{
  "<DASHBOARD_DEMO_READ_KEY>": {
    "name": "ghostlogic-demo-dashboard-reader",
    "tenant": "ghostlogic-demo",
    "role": "reader",
    "scopes": ["read"],
    "created_at": "2026-05-05T00:00:00Z",
    "demo": true
  }
}
```

The dashboard pulls this from env (`DEMO_READ_KEY` or 1Password) at boot. Rotate by editing `keys.json` + bouncing the dashboard.

---

## 4. Rotation playbook

Demo keys must be easy to rotate. Two lever points:

1. **Agent ingester key** (`<DEMO_KEY_VALUE>`):
   - Add a new entry to `/etc/blackbox/keys.json` with the new value (both keys valid for the overlap).
   - `systemctl restart blackbox-api` on the VM.
   - Update `GHOSTLOGIC_DEMO_KEY` in the build/CI/install pipeline.
   - Re-run `python -m logicd demo-dog` on each demo install (writes a new TOML; deletes/overwrites the old one).
   - Confirm new key works (any demo agent's next batch ships green).
   - Delete old entry from `keys.json`, restart again.

2. **Dashboard read key** (`<DASHBOARD_DEMO_READ_KEY>`):
   - Add new entry, restart server.
   - Update dashboard env var (`DEMO_READ_KEY` or equivalent), bounce dashboard.
   - Confirm `/demo` still serves data.
   - Delete old entry from `keys.json`, restart server.

For a cold demo-window cleanup: revoke both keys (delete from keys.json, restart). Demo agents stop posting, `/demo` 401s. No further cleanup required from the user side — the next demo cycle mints fresh values.

---

## 5. What the workstation provides today (already shipped 2026-05-05)

- `logicd demo-dog` subcommand (`logicd/demo.py`, wired in `logicd/__main__.py`).
- `[demo]` block in the config TOML; `Config.demo_mode`, `demo_tenant`, `dashboard_url` round-trip cleanly.
- "Demo Mode" banner printed by `logicd run` when `demo_mode=true`.
- 9 unit tests covering the demo write path + production-isolation check (production `enroll` flow is verified untouched).
- Demo key kept OUT of OS keyring (test enforces this) — rotation = TOML edit, not Credential Manager dance.

What the workstation does NOT do — these are operator-side per this document:

- Mint the demo ingester key in `keys.json`.
- Mint the dashboard reader key in `keys.json`.
- Implement the `/demo` route on the dashboard.
- Set up the `Demo Mode` visual label on the dashboard.

Until items in §1 + §3 are in place, `logicd demo-dog` will produce a config that points at a non-existent server-side key, and the agent will run but every batch will get 401.
