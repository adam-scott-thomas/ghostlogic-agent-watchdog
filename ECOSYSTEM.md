# GhostLogic / Gatekeeper / Recall — Ecosystem Map

Canonical list of packages across the three product families. Source files in each repo carry a short header pointing here. Last revised: 2026-05-08.

## Gatekeeper / Maelstrom Gate

Runtime governance for AI tool access. Open source unless noted.

| Package | Role |
|---|---|
| `gate-keeper` | Core runtime governance and tool-access gate |
| `gate-policy` | Declarative policy engine |
| `gate-sdk` | Developer SDK for agent / tool integration |
| `gate-server` | FastAPI service for Gatekeeper |
| `gate-server-go` | Go implementation of the Gatekeeper service |
| `gate-cli` | Operator CLI |
| `gate-dashboard` | Web dashboard for gate state and decisions |
| `gate-agent` | Governed agent runtime |
| `gate-pilot` | Minimal / demo governed agent |
| `gate-bench` | Benchmark harness |
| `gate-examples` | Integration examples |
| `gate-guard` | Runtime enforcement wrapper *(proprietary)* |
| `gate-metrics` | Prometheus metrics exporter |
| `gate-schema` | Schema validation package |
| `gate-test` | Conformance test suite |
| `gate-webhook` | Webhook notifications |

## GhostLogic

Audit, security, code review, and LLM operations. Mixed open-source and proprietary; see each package.

| Package | Role |
|---|---|
| `gate-compliance` | Compliance audit trail and evidence reports *(proprietary)* |
| `ghostaudit` | Tamper-evident workspace auditor *(proprietary going forward)* |
| `ghostcanary` | Endpoint / security change monitor *(proprietary going forward)* |
| `ghostjury` | Multi-model code-review jury *(proprietary going forward)* |
| `ghostpipe` | Lightweight pipeline runner |
| `ghostprompt` | Versioned prompt registry |
| `ghostrouter` | LLM router with fallback, budgets, and redaction *(proprietary going forward)* |
| `ghostseal` | Audit client for sealing receipts |
| `ghostspine` | Frozen capability registry |
| `ghostlogic-agent-watchdog` | Forensic monitor for AI coding sessions *(proprietary going forward)* |

## Recall

Knowledge capture, distributed via Chrome Web Store (not PyPI/npm).

| Package | Role |
|---|---|
| `recall-page` | Save webpages into Recall-compatible artifacts |
| `recall-session` | Save AI chat sessions into Recall-compatible artifacts |

## Deprecated

These names are retired. Listed for traceability only.

| Old name | Replaced by | Reason |
|---|---|---|
| `maelstrom-gate` | `gate-keeper` | Renamed (squatter on `gatekeeper`) |
| `maelspine` | `ghostspine` | Namespace consolidation; byte-identical source |
| `ControlCore` | `ghostrouter` | Renamed and moved to private development |
| `ghostcore` | `ghostrouter` | Earlier name in same project lineage |
| `ghostpage` | `recall-page` | Re-branded under Recall family |
| `ghostsnip` | `recall-session` | Re-branded under Recall family |

## Discovery commands

- `gate ecosystem` — print this list from any installed `gate-cli`
- Each repo carries a short ecosystem-discovery header in its primary source files
