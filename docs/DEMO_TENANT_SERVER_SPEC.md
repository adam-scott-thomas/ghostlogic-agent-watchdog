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

### 3.1 Dashboard side — already shipped 2026-05-05

The dashboard portion is built in `Ghost_Logic/blackbox-console/`:

- New `src/app/Demo.tsx` shell, mounted by `src/main.tsx` when `window.location.pathname` starts with `/demo`. Production `App.tsx` is untouched.
- New `src/api/demo.ts` client — read-only fetch helpers, **never sends `Authorization`**, no tenant/admin key access.
- Persistent "Demo Mode" banner (sticky, not dismissible). Tenant name surfaces from the API response.
- Live endpoints panel + recent events stream. Both auto-refresh every 5s via `setInterval`.
- 9 unit tests enforcing no-auth-header, no-credentials-in-source, demo-only path prefixing, isolation from production client.
- No `react-router-dom` dependency — single pathname check at mount selects the shell.

The dashboard expects a backend-proxy implementation of three GET endpoints, documented in §3.2. Until those endpoints exist server-side, `/demo` will render the "demo-api unreachable" status strip and empty panels.

### 3.2 Backend contract — the three endpoints the dashboard calls

All three are **public** (no `Authorization` required), **rate-limited per IP**, **tenant-locked server-side to `ghostlogic-demo`**, and **GET-only**. The server is responsible for filtering the response so cross-tenant data cannot leak.

#### `GET /api/v1/demo/status`

Response (200):

```json
{
  "tenant": "ghostlogic-demo",
  "endpoints_active": 3,
  "events_last_hour": 142,
  "server_time": "2026-05-05T00:00:00Z"
}
```

Used to populate the status strip below the banner. `endpoints_active` is the count of demo endpoints with a heartbeat or batch within the last 5 minutes. `events_last_hour` is total events ingested under `tenant=ghostlogic-demo` in the last 60 minutes.

#### `GET /api/v1/demo/endpoints`

Response (200):

```json
[
  {
    "endpoint_id": "11111111-1111-4111-8111-111111111111",
    "endpoint_name": "demo-host-A",
    "hostname": "demo-host-A",
    "last_seen": "2026-05-05T00:00:00Z",
    "events_total": 17
  }
]
```

List of endpoints in the demo tenant. Empty array is a valid response — the UI renders an "onboard via `python -m logicd demo-dog`" hint.

#### `GET /api/v1/demo/buffer/recent?limit=N`

Response (200):

```json
[
  {
    "timestamp": "2026-05-05T00:00:00Z",
    "source_id": "demo-host-A",
    "endpoint_name": "demo-host-A",
    "event_type": "process_start"
  }
]
```

`limit` ≤ 100 server-side enforced. Returned events MUST be redacted of any field that could leak across tenants (no `tenant_id` echoed in the payload, no API-key prefixes, no internal IDs other than `endpoint_id`/`endpoint_name`/`source_id`).

### 3.3 Hard server-side requirements

- **The demo ingester key MUST NOT be embedded in the client.** Browsers should never see it.
- **No user-supplied API key for demo mode.** A visitor lands on `/demo` and sees data. They don't paste anything.
- **Tenant filter is hardcoded at the endpoint, not derived from a request header.** Even if a caller sent an `Authorization` header, it MUST NOT be honoured for /demo paths — those paths read only `ghostlogic-demo` data, regardless of who's asking.
- **Rate limit per IP**: e.g. 30 req/min. Use the existing rate-limit middleware. 429 on excess.
- **CORS**: the dashboard at `blackbox.ghostlogic.tech` should be in the `Access-Control-Allow-Origin` allowlist for these three GET paths only. No credentials forwarded.
- **No admin or tenant key sourcing.** The endpoints either:
  - **Use a hardcoded server-side `tenant_id == "ghostlogic-demo"` filter on the data layer** (no key lookup at all — recommended; fewer moving parts), OR
  - Use a server-side env-baked `DASHBOARD_DEMO_READ_KEY` with role=reader to call the existing read endpoints internally.
  Either is fine. The hardcoded-tenant approach has fewer keys to rotate.
- **No write paths.** Do not add `POST /api/v1/demo/*`. Demo agents POST to the standard `/api/v1/ingest` with the demo ingester key (per §1); the demo dashboard never writes.
- **`Demo Mode` visual label.** Already implemented in `Demo.tsx` (sticky amber banner). Server-side changes should not degrade this framing.

### 3.4 Optional: `DASHBOARD_DEMO_READ_KEY`

If you choose the env-baked-reader-key approach over the hardcoded-tenant-filter approach, mint a separate key in `/etc/blackbox/keys.json`:

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

The API server reads this from env (`DEMO_READ_KEY` or 1Password) on boot and uses it server-side only. Never proxied to the client. Rotate by editing `keys.json` + restarting the API service.

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
