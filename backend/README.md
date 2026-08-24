# Hamdaz 2.0 — Backend

FastAPI + Postgres + Celery. See [`docs/PROJECT_PLAN.md`](../docs/PROJECT_PLAN.md) for the
architecture and the constraints this code enforces.

**Status:** Phases 0–4 and 6 of the backend are built and verified. Phases 5, 7 and 8 are
outlined below under [What is not built yet](#what-is-not-built-yet). The frontend has not
been started.

```
SharePoint write guard (C2) ...... OK
ruff ............................. All checks passed
mypy --strict (55 files) ......... Success
pytest ........................... 222 passed
```

---

## ⚠️ Constraint C2 — live SharePoint is read-only

**SharePoint is in production. This application never writes to it.**

Three independent guards, all of which must stay in place:

| # | Guard | Where |
|---|---|---|
| 1 | **Structural** — the read client has no write methods to call | [`connectors/sharepoint/client.py`](app/connectors/sharepoint/client.py) |
| 2 | **Runtime** — a site-**ID** allowlist containing only the sandbox | [`connectors/sharepoint/guard.py`](app/connectors/sharepoint/guard.py) |
| 3 | **CI** — an AST scan for write calls outside the sandbox module | [`scripts/check_sharepoint_readonly.py`](scripts/check_sharepoint_readonly.py) |

The only writable target in the tenant is `/sites/sandbox` → `sandboxlist`, reachable solely
through [`connectors/sharepoint/sandbox.py`](app/connectors/sharepoint/sandbox.py).

> `/sites/Test` is **not** a sandbox. Despite the name it holds live config — `superusers`,
> `approvers`, `excludeusers` — that the production app reads on every boot.

```bash
uv run python scripts/check_sharepoint_readonly.py    # run before pushing
```

---

## Getting started

```bash
cd infra && docker compose up --build          # postgres, redis, api, worker, beat
```

Without Docker (needs Python 3.12+, Postgres, Redis):

```bash
cd backend
cp .env.example .env                        # fill in the Azure values
uv sync --extra dev
uv run alembic upgrade head
uv run python -m app.cli seed                  # registry + default labels
uv run python -m app.cli bootstrap you@hamdaz.com   # first super admin
uv run uvicorn app.main:app --reload
```

API at http://localhost:8000, docs at `/docs`.

### Commands

```bash
uv run python -m app.cli check                 # verify registries (no DB needed)
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check --fix .
uv run mypy app
uv run alembic revision --autogenerate -m "..."
uv run celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

---

## Layout

```
app/
├── core/
│   ├── config · logging · errors · db · middleware
│   ├── security · oidc · principal          auth and the resolved caller
│   ├── rbac.py                              THE permission registry
│   └── rules/
│       ├── registry.py                      THE decision-point registry
│       ├── evaluator.py                     pure condition/action evaluation
│       └── assignment.py                    pure assignment engine
├── models/     identity · labels · rules · proposals · leave · platform
├── api/v1/     auth · meta · admin · rules · proposals · developer
├── services/   business logic — no HTTP, no ORM leakage
├── connectors/ sharepoint (read-only) + sandbox writer
└── workers/    Celery app, tasks, beat schedule
```

---

## The three registries

Everything in this codebase that could drift between backend, UI and docs is instead declared
**once**, in code, and served to the frontend.

| Registry | Declares | Drives |
|---|---|---|
| [`core/rbac.py`](app/core/rbac.py) | 49 permissions, 6 system roles | `require()`, the role editor, `/meta/permissions` |
| [`core/rules/registry.py`](app/core/rules/registry.py) | 8 decision points with their facts and actions | the evaluator, the rule builder, `/rules/decision-points` |
| [`services/team_service.py`](app/services/team_service.py) | the module list | team module toggles |

Adding a permission is one line in `rbac.py`; the admin UI picks it up automatically.

---

## Authorization

**1. The registry** declares each permission once.
**2. The principal** ([`core/principal.py`](app/core/principal.py)) is built fresh from the
database on every request. Permissions are deliberately **not** in the session token, so a
role change takes effect immediately rather than at token expiry.
**3. `require()`** ([`api/deps.py`](app/api/deps.py)) enforces it, reading `team_id` from the
path so the check is team-aware, and validating the permission key **at wiring time** — a typo
is a startup error, not a mystery 403 months later.

### Scopes

`own` ⊂ `team` ⊂ `all`. The rule that matters most: **a team grant never satisfies an org-wide
check.** A Pre-Sales manager cannot approve Business Development's quotes, and cannot reach a
`Scope.ALL` route. Asserted in [`test_rbac.py`](tests/test_rbac.py) and again at the HTTP
boundary in [`test_api.py`](tests/test_api.py).

### Roles vs labels

Two separate axes, deliberately:

- **Roles grant permission** — what you may do.
- **Labels drive policy** — how the rules engine treats you.

A Senior and a New Joiner can both be `team_member` with identical permissions and receive
very different workloads, because the assignment policy reads their *labels*.

---

## The rules engine (§5.2–5.4)

Root cause #9 in the audit: the legacy system hardcodes its assignment policy in `swp()`, so
changing how work is distributed needs a developer and a deploy.

A **decision point** is a moment where the system makes a choice. Each declares its available
facts and accepted actions, and the rule builder renders only those — so an admin cannot
compose a rule the engine will not honour.

Conditions are **data, not code**: no `eval`, no expression parser. That is the structural
answer to "won't this become a programming language?" — adding an operator means editing
[`evaluator.py`](app/core/rules/evaluator.py).

Three properties are requirements:

| Property | Mechanism |
|---|---|
| **Simulate before publish** | `POST /rules/sets/{id}/simulate` runs the *same* evaluator the live path uses |
| **Versioned & revertible** | every publish snapshots to `rule_set_versions`; revert creates a new version rather than rewinding |
| **Explainable** | every evaluation writes `rule_evaluations` with the facts, matched rules and outcome |

### Assignment (§5.4)

The mechanism behind "a new joiner should get less work" — with no branch anywhere saying
*if new joiner*:

```
capacity  = min multiplier across the person's labels   (new-joiner → 0.4)
load      = open_tasks / capacity
```

A new joiner holding 2 proposals scores like someone holding 5, so the engine stops feeding
them work sooner. `max_open_tasks` gives them a lower hard ceiling too. Busy people fall via
the `open_task_count` factor; `mode: ratio` distributes by label share with rolling-window
drift correction.

`preview` and the live assignment call the **same pure function**, so the admin panel's
"who gets the next 10" is exactly what will happen.

```bash
GET  /api/v1/rules/assignment/{team_id}          # live policy (or the shipped default)
POST /api/v1/rules/assignment/{team_id}/preview  # dry-run, optionally an unsaved draft
POST /api/v1/rules/assignment/{team_id}/publish  # versioned
GET  /api/v1/proposals/workload/{team_id}        # load as the engine sees it
```

---

## What is not built yet

Honest scope. What exists is complete and tested; what follows is not started.

| Area | Plan phase | State |
|---|---|---|
| Quotes, line items, approvals, export | P5 | **Not built.** `quote.*` permissions and the `quote.approval_route` / `quote.validate` decision points exist; no model, service or router |
| Vendors, contacts, customers, partnerships | P5 | **Not built.** Permissions exist |
| Zoho Books connector | P5 | **Not built.** Connection also needs re-authorising |
| Mail client (Graph) | P6 | **Not built.** `email_outbox` and the §8.2 gating exist |
| Supplier email tracking | P6 | **Not built** |
| Document tools (PDF merge, TP, export) | P6 | **Not built** |
| Leave API and service | P7 | **Models built** (`models/leave.py`), and `leave.eligibility` is a decision point; no service or router |
| Data migration from Cosmos / OneDrive | P8 | **Not built** |
| **Frontend (Next.js)** | all | **Not started** — no `frontend/` directory yet |
| Tool builder (`tools`, `tool_runs`) | P3 | **Not built.** Tables are specified in the plan; the observability half of the developer panel is done |
| SSE live activity feed | P3 | **Not built.** Job runs, connector health, rule inspector and audit explorer are done |
| AI | P9 | Deliberately out of scope for v1 (C3) |

### Also outstanding

- **No initial Alembic migration.** Autogenerate needs a live Postgres; run
  `uv run alembic revision --autogenerate -m "initial"` on a machine that has one.
- **Nothing has run against a real database.** All 222 tests are unit/API-level with the DB
  dependency overridden. The service layer's SQL is unexercised.
- **Docker Compose is unrun** — written but not started on a machine with Docker.
- **OIDC is untested against a real tenant.** The flow is implemented with PKCE and full ID
  token verification, but no Azure app registration has been exercised.

---

## Adding a permission

1. One line in `PERMISSIONS` in [`core/rbac.py`](app/core/rbac.py).
2. Grant it to whichever system roles should hold it.
3. `uv run pytest tests/test_rbac.py` — the registry tests catch scope mismatches.
4. `uv run python -m app.cli seed` to reconcile the database.

The seed runs code → database, never the reverse.
