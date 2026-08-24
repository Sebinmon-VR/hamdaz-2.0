# Hamdaz 2.0 — Project Plan

**Status:** Draft v2 · **Date:** 2026-08-23 · **Target repo:** `hamdaz-2.0` (empty, greenfield)

### Standing constraints

| # | Constraint | Meaning |
|---|---|---|
| C1 | **Legacy repo is live** — `C:\Users\ansha\Hamdaz-` | Read-only reference. No file in it is edited, ever. |
| C2 | **Live SharePoint is read-only** | Hamdaz 2.0 **never writes or pushes to any live SharePoint site**. Read-only ingest only. The single writable target is the dedicated sandbox site — `/sites/sandbox` (§8.1). Enforced in code, config and CI. |
| C3 | **AI is out of scope for v1** | The AI assistant, procurement AI and AI document analysis are deferred to Phase 9, after cut-over. |
| C4 | **Pinecone is not used** | Confirmed dead in the legacy system. Not carried forward. |

---

## 1. Executive summary

Hamdaz 2.0 replaces the existing single-team Flask ERP with a multi-team platform built on
**FastAPI + Postgres + Next.js**. Five goals drive the rebuild:

1. **Multi-team.** The old system served one team, with roles hardcoded in an Excel file on
   OneDrive. The new one supports arbitrary teams, each with managers, members and custom roles.
2. **Admin control plane.** Create teams, define roles and permissions, assign members and
   configure modules per team — from a UI, with no code deploys.
3. **Rules on everything.** Admins configure the *policies* the system runs on — how tasks are
   assigned, who approves what, how leave is granted — without a developer. §5.2–§5.4.
4. **Developer panel.** Live visibility into every background job, connector, request and user
   action, plus the ability to define new automation tools without shipping code.
5. **Own the data.** Postgres becomes the source of truth. SharePoint, Zoho and Microsoft Graph
   become *read connectors*, not the database itself.

### 1.1 Explicitly out of scope for v1

| Deferred | Why | When |
|---|---|---|
| AI personal assistant | Not needed now | Phase 9 |
| Procurement AI (analyze, draft email, find distributors) | Not needed now | Phase 9 |
| AI document analysis (`/analyze`, `/rfq_vs_quotes` comparison) | Not needed now | Phase 9 |
| AI summarisation of supplier email replies | Tracking still ships; only the summary is deferred | Phase 9 |
| pgvector / embeddings | Only exists to serve AI retrieval | Phase 9 |
| Pinecone | Dead in the legacy system (C4) | Never |
| **Any** SharePoint write | SharePoint is live (C2) | Cut-over decision, §9 P8 |

Non-AI document tools **do** ship in v1: PDF merge, technical proposal generation, and quote
export to docx/xlsx. None of them call an LLM.

---

## 2. Legacy system audit

Measured from `C:\Users\ansha\Hamdaz-` @ `63ed3bb` (branch `main`).

### 2.1 What is there

| Metric | Value |
|---|---|
| Python LOC | ~7,900 across 9 modules |
| `app.py` | 3,022 lines, **94 routes**, no blueprints, no service layer |
| `sharepoint_items.py` | 2,323 lines — Graph API, Excel I/O, analytics, rotation, all mixed |
| `cosmos.py` | 1,361 lines — 10 containers, hand-rolled queries |
| Templates | 43 Jinja files |
| Largest template | `pages/personal_assistant.html` — 2,874 lines, **2,184 of them inline JS** |
| Deploy | Azure App Service, gunicorn, Flask 2.0.1 (2021), Python 3.11 |

### 2.2 Why it needs rebuilding — root causes, not symptoms

| # | Problem | Evidence | Consequence |
|---|---|---|---|
| 1 | **Roles live in a OneDrive Excel file** | `index()` reads `get_user_details_from_excell()`, branches on `role`, picks a template by name | Cannot express teams, managers, or per-module permission. Adding a role = editing a spreadsheet and an `if/elif` chain. |
| 2 | **Superusers/approvers pulled from SharePoint at boot** | `SUPERUSERS = superusers_from_sl()` at import time, refreshed by a thread | Authorization state is global, eventually-consistent, unauditable. |
| 3 | **Global mutable state as the database** | Module-level `tasks`, `tasks_dict`, `df`, `user_analytics` mutated by a background thread | Not safe across gunicorn workers; each holds a divergent copy. Scaling out corrupts analytics. |
| 4 | **Background jobs inside the web process** | Three `while True` + `sleep()` threads (60 s, 45 s, 1800 s) | Every web worker runs its own copy → duplicated SharePoint writes and duplicated emails. No retry, no visibility, no dead-letter. |
| 5 | **No single source of truth** | Data split across SharePoint lists, OneDrive Excel, Cosmos DB (10 containers) and Zoho Books | Cross-entity queries impossible; joins done in pandas, in-process. |
| 6 | **Three competing CSS systems** | `base.html` loads Bootstrap 5.3 CDN **and** Tailwind 2.2 CDN, while `package.json` builds Tailwind 3.4 locally | Inconsistent UI, huge payload, specificity wars. The "bad UI" root cause. |
| 7 | **No tests, no types, no migrations** | `tests/` holds one ad-hoc script and two `.txt` dumps | Every change is a production experiment. |
| 8 | **No audit trail** | No action log anywhere | Cannot answer "who approved this quote?" or "why was this assigned to Rahul?" — a hard blocker for both the rules engine and the developer panel. |
| 9 | **Assignment logic is hardcoded** | `swp()`, `calculate_priority_score()`, `assign_priority_rank()` embed the rotation policy in Python | Changing how work is distributed requires a developer and a deploy. This is exactly what §5.4 fixes. |

### 2.3 Confirmed dead code — do **not** port

Verified by cross-referencing every `def` against all `.py` and `.html` files.

**Never called (26 functions):**

- `sharepoint_items.py` (18): `compute_user_analytics`, `ensure_sharepoint_folder`,
  `extract_usernames_from_df`, `fetch_filtered_sharepoint_data`, `fetch_user_planner_tasks`,
  `generate_sharepoint_filter_endpoint`, `get_all_customers_from_onedrive`, `get_list_columns`,
  `get_teams_stauts`, `get_user_details`, `get_user_profile_photo`,
  `get_user_tasks_details_from_excell`, `get_user_teams_chats`, `list_org_users`,
  `save_distributors_data_to_sharepoint`, `send_quote_approval_email`, `update_sharepoint_item`,
  `upload_file_to_sharepoint`
- `cosmos.py` (2): `get_item_distributors`, `get_user_leave_count`
- `zoho.py` (4): `fetch_sales_orders`, `get_purchase_orders`, `structure_items_data`,
  `structure_quotes_data`
- `sharepoint_data.py` (1): `get_user_display_name`
- Plus `quote_generator.py` helpers reachable only from a commented-out route

**Dead dependency — Pinecone.** `app.py` imports it, constructs the client, and creates
`index = pc.Index("hamdaz")`. There is **no `index.query` or `index.upsert` call anywhere in the
repo.** It is a live API key and a paid vendor doing nothing. Dropped entirely (C4).

**Shadowed duplicate definitions** (the first is unreachable — a latent bug):

- `sharepoint_items.py` → `generate_quote_excel` at L1209 **and** L1770
- `zoho.py` → `fetch_items` at L81 **and** L244

**Orphan templates** (no `render_template` reference): `ai_tasks.html`, `datasheets.html`,
`generate_profile.html`, `business_dev_team.html`, `customer_success_dashboard.html`,
`pages/chat.html`, `pages/chatbot.html`, `pages/assistant.html`,
`pages/components/{progress,form}.html`, `competitor_profile.html`, `pg.html`.

**Other removals:** commented-out `@app.route("/line_items")`, ~61 commented-out code lines, 46
`print()` calls surviving alongside the newer `logger.py`, the `scratch/` directory,
`tests/test_out*.txt`, and the committed `.venv/` + `node_modules/`.

### 2.4 Functional module inventory — the porting checklist

19 areas extracted from the 94 routes.
**Port** = logic is sound, move it into a tested service.
**Rebuild** = the legacy approach is the problem; redesign it.
**Defer** = Phase 9, after cut-over.

| # | Module | Routes | Data source | Disposition |
|---|---|---|---|---|
| 1 | Auth & onboarding | 4 | Azure AD + Excel | **Rebuild** |
| 2 | Role-routed dashboards | 3 | Excel role string | **Rebuild** |
| 3 | Proposals & task assignment | 2 | SharePoint `Proposals` | **Port** (read-only ingest) |
| 4 | Workload rotation & priority | — | SharePoint `useranalytics` | **Rebuild as rules engine** (§5.4) |
| 5 | Business Development / partnerships | 2 | SharePoint | **Port** (read-only ingest) |
| 6 | Customer Success | 1 | SharePoint | **Port** (stub in legacy) |
| 7 | Quotes & approvals | 6 | Cosmos + Zoho | **Port** |
| 8 | Vendors & distributors | 3 | Cosmos + Zoho | **Port** |
| 9 | Contacts & business cards | 3 | OneDrive Excel | **Port** (one-time import) |
| 10 | Supplier email tracking | 2 | Cosmos + Graph | **Port**, minus AI summary |
| 11 | Mail client | 7 | Graph | **Port** |
| 12 | AI personal assistant | 6 | Cosmos + OpenAI | **Defer → P9** |
| 13 | Procurement AI | 5 | Cosmos + OpenAI | **Defer → P9** |
| 14 | Shared projects & collaboration | 7 | Cosmos | **Port** |
| 15 | Notifications | 2 | Cosmos | **Rebuild** |
| 16 | Document tools | 6 | Files (+ OpenAI) | **Split**: merge/TP/export **Port**; analyze + RFQ-vs-quote **Defer → P9** |
| 17 | Leave management | 14 | Cosmos | **Port + rules** |
| 18 | Reports | 3 | pandas | **Rebuild** |
| 19 | Zoho Books sync | — | Zoho → Cosmos | **Rebuild as connector** |

---

## 3. Target architecture

```
┌─────────────────────────────────────────────────────────────┐
│  frontend/  Next.js 15 · TypeScript · Tailwind 4 · shadcn/ui │
│  (admin) (developer) (teams) (proposals) (quotes) (leave)    │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST + SSE (typed client from OpenAPI)
┌───────────────────────────┴─────────────────────────────────┐
│  backend/  FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic    │
│  ├─ api/          routers, one per domain                    │
│  ├─ core/         auth · rbac · rules · audit · settings     │
│  ├─ services/     business logic (no HTTP, no ORM leakage)   │
│  ├─ connectors/   sharepoint(RO) · graph · zoho · blob       │
│  ├─ models/       SQLAlchemy ORM                             │
│  └─ workers/      Celery tasks + beat schedule               │
└───────┬──────────────────────────────┬──────────────────────┘
        │                              │
┌───────┴────────┐            ┌────────┴─────────┐
│  Postgres 16   │            │  Redis           │
│  source of     │            │  broker · cache  │
│  truth         │            │  pub/sub (SSE)   │
└────────────────┘            └──────────────────┘
        ▲
        │  ONE-WAY sync workers — read only, idempotent, retried, audited
        │  ╳ no write path exists back to SharePoint (C2)
┌───────┴──────────────────────────────────────────────────────┐
│ SharePoint (READ ONLY) · MS Graph (mail/users) · Zoho Books   │
│ OneDrive Excel (one-time import) · Azure Blob (documents)     │
└───────────────────────────────────────────────────────────────┘
```

### 3.1 Key architectural decisions

| Decision | Choice | Rationale |
|---|---|---|
| Backend | FastAPI + Pydantic v2 | Keeps Python integration logic; async suits Graph/Zoho fan-out; free OpenAPI → typed frontend client |
| DB | Postgres 16 + SQLAlchemy 2 + Alembic | Real FKs, transactions, row-level scoping, versioned migrations |
| Background work | **Celery + Redis, separate process** | Kills root cause #4. Retries, dead-letter, and a run history the developer panel reads |
| Frontend | Next.js 15 App Router + Tailwind 4 + shadcn/ui | One CSS system (kills root cause #6); RSC for fast dashboards |
| Auth | Azure AD OIDC → own JWT session | Keep Microsoft SSO; authorization moves to Postgres, out of Excel |
| **Policy** | **Declarative rules engine in Postgres** (§5.2) | Kills root cause #9. Admins change assignment/approval/leave policy with no deploy |
| **SharePoint** | **Read-only connector, no write methods exist** | C2. Enforced three ways — §8.1 |
| Cosmos DB | **Retire.** All 10 containers → Postgres | Removes the second database and its hand-rolled query strings |
| Pinecone | **Drop.** Confirmed dead | C4 |
| Vector search | **Deferred to P9** (pgvector when AI lands) | No AI in v1, so no retrieval need |
| Documents | Azure Blob Storage | Replaces ad-hoc OneDrive/SharePoint file writes — which C2 forbids anyway |
| Realtime | SSE over Redis pub/sub | Presence, notifications, developer live-feed without a WebSocket server |

### 3.2 Repository layout

```
hamdaz-2.0/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI factory, middleware, lifespan
│   │   ├── core/
│   │   │   ├── config.py            # pydantic-settings, typed env
│   │   │   ├── security.py          # OIDC, JWT, session
│   │   │   ├── rbac.py              # permission registry + dependencies
│   │   │   ├── rules/               # decision-point registry + evaluator  ◄ §5.2
│   │   │   ├── audit.py             # audit_log writer
│   │   │   └── errors.py            # RFC7807 problem responses
│   │   ├── models/                  # SQLAlchemy ORM
│   │   ├── schemas/                 # Pydantic request/response
│   │   ├── api/v1/                  # routers per domain
│   │   ├── services/                # business logic, unit-testable
│   │   ├── connectors/
│   │   │   ├── sharepoint/          # READ ONLY — no write methods (§8.1)
│   │   │   ├── graph_mail/  graph_users/  zoho/  blob/
│   │   └── workers/                 # celery_app.py, tasks/, beat_schedule.py
│   ├── alembic/
│   ├── tests/                       # pytest: unit + integration (testcontainers)
│   └── pyproject.toml               # uv / ruff / mypy / pytest
├── frontend/
│   ├── app/
│   │   ├── (auth)/                  # login, callback
│   │   ├── (app)/
│   │   │   ├── dashboard/ proposals/ quotes/ vendors/ contacts/ mail/ leave/
│   │   │   ├── admin/               # ADMIN PANEL + RULES  §5
│   │   │   └── developer/           # DEVELOPER PANEL      §6
│   ├── components/ui/               # shadcn — the single design system
│   └── lib/api/                     # generated OpenAPI client
├── infra/
│   ├── docker-compose.yml           # postgres, redis, backend, worker, beat, frontend
│   └── azure/                       # bicep / app service config
├── docs/
└── .github/workflows/               # ci.yml (lint+type+test+sp-write-guard), deploy.yml
```

---

## 4. Multi-team & RBAC model

### 4.1 Concepts

```
Organization (Hamdaz)
 └── Team              e.g. Pre-Sales, Business Development, Customer Success, Procurement
      └── Membership   (user × team × role)
           └── Role    system-defined or admin-created
                └── Permission[]   granular, e.g. "quotes.approve"
      └── Labels       user categories that rules read — e.g. New Joiner, Senior  (§5.3)
```

- A user belongs to **many** teams, with a **different role in each**.
- A **Team** enables a set of **modules**. A team only sees what it has enabled.
- **Permissions are scoped**: `own` (my rows), `team` (my team's rows), `all` (org-wide).
- **Roles grant permission. Labels drive policy.** They are separate axes — a Senior and a New
  Joiner may both be `team_member` (same permissions) but get very different workloads (§5.4).

### 4.2 Built-in roles

| Role | Scope | Purpose |
|---|---|---|
| `super_admin` | Org | Everything, including admin + developer panels |
| `developer` | Org | Developer panel, tool builder, connector config, logs. **No business-data writes** |
| `org_manager` | Org | Cross-team reports, approvals, leave oversight |
| `team_manager` | Team | Manage own team's members, assignments, approvals; edit team-scoped rules |
| `team_member` | Team | Do the work |
| `viewer` | Team | Read-only |
| *custom* | Team/Org | Composed from the permission registry in the admin UI |

### 4.3 Permission registry (illustrative)

```
proposals.{read,create,update,delete,assign,reassign}
quotes.{read,create,update,submit,approve,reject,export}
vendors.{read,create,update}  contacts.{read,update}
leave.{request,cancel,read_own,read_team,approve,configure}
mail.{read,send,delete}
reports.{read_own,read_team,read_org}
rules.{read, edit_team, edit_org, simulate, publish}
labels.{read, assign, manage}
admin.{teams.manage, users.manage, roles.manage, modules.configure}
dev.{panel.view, logs.read, jobs.manage, tools.create, connectors.configure, flags.manage}
```

Declared once in `core/rbac.py`, enforced by a FastAPI dependency
(`require("quotes.approve", scope="team")`), and rendered as checkboxes in the admin role editor.
**One registry drives enforcement, the UI and the docs** — they cannot drift apart.

### 4.4 Migration from legacy roles

| Legacy | Becomes |
|---|---|
| `SUPERUSERS` SharePoint list | Membership rows with `super_admin` |
| `approvers` SharePoint list | `quotes.approve` permission |
| Excel `role = "pre-sales"` | Team *Pre-Sales* + `team_member` |
| Excel `role = "business development"` | Team *Business Development* + `team_member` |
| Excel `role = "customer success"` | Team *Customer Success* + `team_member` |
| Excel `role = "ai"` | `developer` role |
| Excel `flag = 1` | `user.status = 'active'` |
| `EXCLUDED_USERS` list | Label `excluded-from-rotation` (§5.3) |

---

## 5. Admin panel & rules engine

Route group `frontend/app/(app)/admin/`. Gated on `admin.*` / `rules.*` / `labels.*`.

### 5.1 Screens

| Screen | Capabilities |
|---|---|
| **Teams** | Create/rename/archive; set team lead; enable/disable modules per team; team settings |
| **Members** | Invite from Azure AD; assign to teams; set role per team; bulk actions; deactivate; view a member's team/role/label matrix |
| **Roles & permissions** | Create custom roles; permission matrix editor grouped by module with `own`/`team`/`all` scope; clone a role; see who holds a role before changing it |
| **Labels** | Create and manage user category labels; assign manually or by auto-rule — §5.3 |
| **Rules** | The rules engine UI: pick a decision point, build conditions and actions, simulate, publish — §5.2 |
| **Assignment policy** | The dedicated builder for `proposal.assign` — capacity, ratios, eligibility, preview — §5.4 |
| **Approval workflows** | Who approves what, in what order, with fallbacks and thresholds. Replaces the hardcoded `approvers` list |
| **Leave configuration** | Holidays, leave types, concurrency rules, approval chain, blackout periods |
| **Modules** | Toggle modules org-wide or per team; configure module settings |
| **Data connectors** | SharePoint/Zoho/Graph health, last sync, record counts; trigger a resync. **SharePoint shows a permanent READ-ONLY badge** |
| **Audit log** | Filterable, exportable log of every mutating action: who, what, when, before→after |
| **Organization settings** | Branding, domains, notification defaults, session policy |

> **Design rule.** Every one of these must be doable in the UI. If an admin has to ask a developer
> to edit a SharePoint list or a spreadsheet, we have rebuilt the old system.

### 5.2 The rules engine

The requirement is *"the admin can set rules on everything."* The way to deliver that without
building a hundred bespoke settings screens is a single declarative engine evaluated at named
**decision points**.

A decision point is a moment where the system makes a choice. Each one declares:

- the **facts** available in its context (a typed schema),
- the **actions** it accepts,
- and a **default policy** shipped with the product.

The admin UI reads that registry and renders a builder offering *only valid* conditions and
actions — the same single-registry pattern used for permissions (§4.3), so the UI can never drift
from what the engine actually supports.

| Decision point | Fires when | Actions the admin can configure |
|---|---|---|
| `proposal.assign` | A proposal arrives or is reassigned | Pick assignee, set priority, notify — **§5.4** |
| `proposal.escalate` | Bid closing date approaches, or SLA breached | Notify, reassign, raise priority, flag to manager |
| `proposal.validate` | Before status change | Require fields, block, warn |
| `quote.approval_route` | A quote is submitted | Choose approver chain by amount, margin, customer, team |
| `quote.validate` | Before submit | Require attachments/fields, enforce margin floor |
| `leave.eligibility` | Leave is requested | Auto-approve, require approval, block, cap concurrency |
| `leave.handoff` | Leave is approved | Pick who inherits the ongoing proposals (reuses `proposal.assign`) |
| `user.label` | User joins, or their attributes change | Grant/revoke labels automatically — §5.3 |
| `notification.route` | Any domain event | Who is notified, on which channel, how urgently |
| `visibility.field` | A record is rendered | Mask or hide fields by label or role |

**Rule shape.** Every rule is `conditions → actions`, ordered, with the first match winning unless
the set is marked `evaluate_all`:

```yaml
decision_point: quote.approval_route
team: pre-sales
rules:
  - name: Large deals go to the MD
    conditions:
      all:
        - {fact: quote.total, op: ">=", value: 500000}
        - {fact: quote.currency, op: "=", value: "AED"}
    actions:
      - {type: require_approval, approver_role: org_manager}
      - {type: notify, target: team_lead}

  - name: Thin margin needs a second look
    conditions:
      all:
        - {fact: quote.margin_pct, op: "<", value: 12}
    actions:
      - {type: require_approval, approver_role: team_manager}

  - name: Default
    conditions: {always: true}
    actions:
      - {type: auto_approve}
```

**Three properties make this trustworthy, and all three are requirements, not nice-to-haves:**

1. **Simulate before publish.** Every rule set can be dry-run against real current data. The admin
   sees exactly what *would* happen — which quotes route where, who *would* get the next ten
   proposals — before anything takes effect.
2. **Versioned and revertible.** Publishing creates a new version. Every version is diffable and
   revertible, with the author and timestamp recorded.
3. **Explainable.** Every evaluation writes a `rule_evaluations` row capturing the input facts,
   which rules matched, and the outcome. So both admin and developer panel can answer *"why did
   Rahul get this proposal?"* with the actual decision trace — something the old system can never do.

### 5.3 User category labels

Labels are the vocabulary rules speak in. A label is an admin-created tag on a user, optionally
scoped to a team.

| Kind | Examples | Typically drives |
|---|---|---|
| `category` | New Joiner, Senior, Mid, Junior, Team Lead, Part-time | Capacity, ratios, approval routing |
| `skill` | Networking, Security, CCTV, Fire Alarm | Eligibility — only tag-matching people get certain work |
| `status` | On Probation, Excluded from Rotation, On Notice | Eligibility, capacity |

- Labels can be **assigned manually** or **granted automatically** by a `user.label` rule —
  e.g. *joined within 90 days → New Joiner*, with an `expires_at` so it falls off by itself.
- The legacy `EXCLUDED_USERS` list becomes the `excluded-from-rotation` status label.
- Labels are referenced by capacity multipliers, eligibility filters, ratio targets, approval
  routing and field visibility.

### 5.4 Assignment rules — the worked example

This is the case you raised, so it gets the full treatment. The `proposal.assign` decision point
has a dedicated builder because it is the most-used policy in the system.

```yaml
policy: pre_sales_default          # team-scoped, versioned, simulatable
team: pre-sales

# ─── 1. ELIGIBILITY — hard filters, applied first ───────────────────
eligibility:
  member_of_team: true
  not_on_leave: true
  not_labelled: [excluded-from-rotation, on-notice]
  requires_labels: []              # e.g. [security] for security proposals
  max_open_tasks:
    default: 8
    by_label: {new_joiner: 3, part_time: 4}

# ─── 2. CAPACITY — "a new joiner should get less work" ──────────────
capacity:
  default: 1.0
  by_label:
    new_joiner:   0.4              # carries 40% of a normal load
    on_probation: 0.3
    part_time:    0.5
    team_lead:    0.6              # has management duties too
    senior:       1.0

# ─── 3. DISTRIBUTION — how the winner is chosen ─────────────────────
distribution:
  mode: weighted_least_loaded      # least_loaded | round_robin | ratio
                                   # | weighted_least_loaded | manual
  factors:
    load_vs_capacity:       {weight: 0.45, direction: lower_is_better}
    open_task_count:        {weight: 0.30, direction: lower_is_better}
    days_since_last_assign: {weight: 0.25, direction: higher_is_better}

  # when mode: ratio — "assign like in ratios"
  ratio:
    by: label
    targets: {senior: 3, mid: 2, junior: 1}
    window: rolling_30d            # drift measured over this window and corrected

# ─── 4. TIE-BREAK & FALLBACK ────────────────────────────────────────
tie_break: longest_idle
on_no_eligible_candidate: notify_manager   # never silently drop work

# ─── 5. OVERRIDE ────────────────────────────────────────────────────
allow_manual_override: true        # a manager can reassign; reason required, audited
```

**How this satisfies each thing you asked for:**

| Your requirement | Mechanism |
|---|---|
| "a new joinee to be less task assigned" | `capacity.by_label.new_joiner: 0.4` — combined with the `load_vs_capacity` factor, a new joiner holding 2 tasks scores as effectively 5, so the engine stops feeding them work sooner. No special-casing in code. |
| "multiple job assigned persons to be less task assigned" | The `open_task_count` factor pushes busy people down the ranking, and `max_open_tasks` is a hard ceiling they cannot exceed. |
| "assign like in ratios" | `mode: ratio` with `targets`, measured over a rolling window so short-term luck self-corrects. |
| "rules on category labels of users" | §5.3 — labels are first-class, and every clause above (`eligibility`, `capacity`, `ratio`) can key off them. |
| "other areas if possible" | The same engine covers the ten decision points in §5.2 — approvals, escalation, leave, notifications, visibility. |

**Preview is mandatory.** Before publishing, the builder shows: the next ten assignments under the
new policy, current distribution versus target ratio, per-member effective load, and a diff against
the currently live policy. An assignment policy nobody can predict is an assignment policy nobody
will trust.

---

## 6. Developer panel

Route group `frontend/app/(app)/developer/`. Gated on `dev.*`.

### 6.1 Observability — "see all the running things"

| Screen | What it shows |
|---|---|
| **Live activity feed** | SSE stream of every request, job start/finish and connector call — filterable by user, team, module, status. The old system has zero of this. |
| **Jobs & schedules** | Every Celery task: schedule, last run, duration, status, retry count, args, result/traceback. Actions: run now, pause, retry, cancel, replay with same args |
| **Connector health** | Per connector: up/down, latency p50/p95, error rate, rate-limit headroom, token expiry, last successful sync, delta cursor. **SharePoint is badged READ-ONLY and its write-guard status is displayed** |
| **Rule inspector** | Live view of `rule_evaluations`: which decision points fired, what facts they saw, which rules matched, what they decided. The debugger for §5.2 |
| **Request tracing** | Per-request timeline: route → service → DB queries → outbound calls, with timings. Correlation ID surfaced in error responses |
| **Error inbox** | Grouped exceptions with counts, first/last seen, stack trace, affected users; mark resolved |
| **Audit explorer** | The full `audit_log` with before→after diffs, exportable |
| **Metrics** | Request rate/latency/errors, queue depth, DB pool, cache hit rate |

### 6.2 Tool builder — "create tools and many options"

A registry that lets a developer define a callable capability **without a deploy**:

```yaml
name: find_overdue_proposals
description: List proposals past their bid closing date for a team
scope: team
permission: proposals.read
parameters:                    # JSON Schema → validated + rendered as a form
  team_id:      {type: string,  required: true}
  days_overdue: {type: integer, default: 0}
implementation:
  type: sql_query              # sql_query | http_request | connector_call
  query: |
    SELECT ... FROM proposals
    WHERE team_id = :team_id AND bcd < now() - :days_overdue
surfaces:
  - ui_action                  # a button on the Proposals page
  - scheduled                  # runs on a cron; result notified
  - report                     # appears in the reports list
```

| Type | What it does |
|---|---|
| `sql_query` | Parameterised query against a **read-only** Postgres role. Bound parameters only — never string interpolation |
| `http_request` | Calls an internal or approved external endpoint, against a domain allowlist |
| `connector_call` | Invokes a registered connector method with a declared permission requirement. **SharePoint write methods do not exist to be called** (§8.1) |
| ~~`pipeline`~~ | *Deferred to P9* — this is the LLM-composing type, and there is no AI in v1 |

Supporting screens: **feature flags** (per org/team/user rollout), **API keys & webhooks**, a
**read-only DB console**, and **connector config** with secrets in Azure Key Vault, never rendered
back to the browser.

### 6.3 Safety rails

Tool creation is a privilege-escalation surface, so it is constrained by design:

- Tools execute under the **calling user's** permissions, never the author's.
- SQL runs as a read-only Postgres role.
- Write tools require an explicit `connector_call` with a declared permission and `super_admin`
  approval before activation.
- **No SharePoint write path exists to invoke** — the connector has no write methods (C2).
- No arbitrary Python execution. Ever.
- Every run is audited, rate-limited and timed out.
- Tools are versioned; each version diffable and revertible.

---

## 7. Data model

```
-- ─── identity & access ───────────────────────────────────────────
users(id, azure_object_id, email, display_name, photo_url, status, joined_at, created_at)
teams(id, slug, name, description, lead_user_id, enabled_modules jsonb, settings jsonb, archived_at)
roles(id, team_id NULL, key, name, is_system, description)
permissions(key, module, description)              -- seeded from the registry
role_permissions(role_id, permission_key, scope)   -- scope: own|team|all
memberships(id, user_id, team_id, role_id, joined_at, UNIQUE(user_id, team_id))

-- ─── labels & rules  (§5.2–5.4) ──────────────────────────────────
labels(id, key, name, kind, color, description, team_id NULL)   -- kind: category|skill|status
label_assignments(id, user_id, label_id, team_id NULL, assigned_by,
                  granted_by_rule_id NULL, expires_at, created_at,
                  UNIQUE(user_id, label_id, team_id))
rule_sets(id, decision_point, team_id NULL, name, enabled, priority,
          version, published_by, published_at, evaluate_all bool)
rules(id, rule_set_id, position, name, conditions jsonb, actions jsonb, enabled)
rule_set_versions(id, rule_set_id, version, snapshot jsonb, author_id, created_at)
rule_evaluations(id, decision_point, rule_set_id, rule_set_version, entity_type, entity_id,
                 facts jsonb, matched_rule_ids jsonb, outcome jsonb,
                 actor_id, simulated bool, duration_ms, created_at)
assignment_policies(id, team_id, name, version, eligibility jsonb, capacity jsonb,
                    distribution jsonb, tie_break, fallback, active, published_by, published_at)

-- ─── core business ───────────────────────────────────────────────
proposals(id, team_id, external_ref, title, customer_id, status, submission_status,
          assigned_to, previous_owner, bcd, priority_score, created_at, updated_at,
          source, source_id)                       -- source_id = SharePoint item id
proposal_events(id, proposal_id, actor_id, type, payload jsonb, created_at)
quotes(id, team_id, proposal_id, zoho_estimate_id, customer_id, currency, subtotal, tax,
       total, margin_pct, status, created_by, created_at)
quote_items(id, quote_id, sku, description, quantity, rate, discount, line_total)
approvals(id, entity_type, entity_id, step, approver_id, status, remarks,
          routed_by_rule_id NULL, decided_at)
vendors(id, name, zoho_id, contact jsonb, metadata jsonb)
item_distributors(id, item_sku, vendor_id, source, confidence, last_seen_at)
contacts(id, name, company, email, phone, source, metadata jsonb)
customers(id, zoho_id, name, metadata jsonb)
partnerships(id, product_group, product, manufacturer, competitor, status, fields jsonb)

-- ─── supplier email ──────────────────────────────────────────────
tracked_emails(id, proposal_id, message_id, tracking_id, to_email, subject, body,
               sent_by, replied_at, reply_body, status)     -- reply_summary → P9
supplier_quotes(id, proposal_id, tracking_id, supplier_email, parsed jsonb, status)

-- ─── collaboration ───────────────────────────────────────────────
shared_projects(id, proposal_id, created_by, created_at)
project_members(project_id, user_id, invited_by, accepted_at)
project_messages(id, project_id, user_id, content, created_at)
presence(user_id, project_id, last_seen_at)
notifications(id, user_id, type, title, body, entity_type, entity_id,
              routed_by_rule_id NULL, read_at, created_at)

-- ─── leave ───────────────────────────────────────────────────────
leave_requests(id, user_id, team_id, type, start_date, end_date, days, reason,
               status, approver_id, remarks, handoff_to, decided_at, created_at)
leave_settings(id, team_id NULL, key, value jsonb)
holidays(id, team_id NULL, title, start_date, end_date, type)

-- ─── documents ───────────────────────────────────────────────────
documents(id, team_id, entity_type, entity_id, blob_url, filename, mime, size, uploaded_by)

-- ─── platform / developer panel ──────────────────────────────────
audit_log(id, actor_id, team_id, action, entity_type, entity_id, before jsonb,
          after jsonb, ip, user_agent, correlation_id, created_at)
job_runs(id, task_name, args jsonb, status, started_at, finished_at, duration_ms,
         error, retries, correlation_id)
connector_status(name, mode, healthy, last_success_at, last_error, latency_ms, cursor jsonb)
                                                   -- mode: read_only | read_write
tools(id, name, description, scope, permission_key, parameters jsonb,
      implementation jsonb, surfaces jsonb, version, active, created_by, approved_by)
tool_runs(id, tool_id, actor_id, params jsonb, result jsonb, status, duration_ms, created_at)
feature_flags(key, description, rules jsonb, updated_by, updated_at)

-- ─── DEFERRED to Phase 9 (AI) ────────────────────────────────────
-- ai_sessions, ai_messages, prompts, embeddings(vector)  -- pgvector arrives with P9
```

**Notes**

- Every synced entity keeps `source` + `source_id` so a SharePoint or Zoho record maps 1:1 and
  ingest stays idempotent.
- `audit_log` is append-only; `rule_evaluations` is its policy-side twin. Together they answer
  both *"who did this?"* and *"why did the system do this?"*
- All team-scoped tables carry `team_id`; a SQLAlchemy query filter applies the caller's scope
  automatically, so no router can leak another team's rows.

---

## 8. Connector layer

One base interface, one implementation per source, all running **in workers only** — never in a
request handler.

| Connector | Mode | Replaces | Responsibilities |
|---|---|---|---|
| `sharepoint` | **READ ONLY** on every live site | `sharepoint_items.py` + `sharepoint_data.py` | Delta-syncs `Proposals`, partnership and config lists **into** Postgres. Delta cursor lives in `connector_status`, not a module global |
| `sharepoint_sandbox` | Read + write, **sandbox site only** | — | Write-path integration testing against `/sites/sandbox` → `sandboxlist`. Hard-bound to the sandbox site ID at construction — §8.1.1 |
| `graph_mail` | Read + send | Mail routes in `app.py` | Inbox listing, send, drafts, supplier-reply matching. **See §8.2 — sending is gated in non-production** |
| `graph_users` | Read only | `get_all_users`, `list_org_users` | Directory sync → `users` for admin invitations |
| `zoho_books` | Read (write at cut-over) | `zoho.py` + `sync_zoho_to_cosmos.py` | Items, estimates, POs, customers → Postgres |
| `onedrive_excel` | **Read once, then delete** | `get_*_from_onedrive` | One-time import of contacts/customers/users. The Excel dependency is then removed entirely |
| `blob` | Read + write | Ad-hoc SharePoint/OneDrive file writes | Document storage with signed URLs. This is where files go now that C2 forbids SharePoint uploads |
| ~~`openai`~~ | — | — | *Deferred to P9* |

**Every connector:** typed config, health check, exponential-backoff retry, circuit breaker,
structured logging with a correlation ID, and a row in `connector_status`.

### 8.1 Enforcing the SharePoint read-only rule (C2)

The legacy system has **13 SharePoint/OneDrive write paths**, several on hot code paths — the
60-second background thread calls `update_user_analytics_in_sharepoint` and
`add_item_to_sharepoint` on every tick, from every worker. None of these may exist in 2.0 while
SharePoint is live.

Legacy write functions, all of which are *deliberately not ported*:

| Function | Wrote to |
|---|---|
| `update_user_analytics_in_sharepoint` | `useranalytics` list — **every 60 s, per worker** |
| `add_item_to_sharepoint` | `useranalytics` list |
| `delete_user_from_useranalytics` | `useranalytics` list |
| `add_sharepoint_list_item` | `Proposals` list |
| `update_sharepoint_item` / `update_sharepoint_item_with_link` | list items |
| `handoff_proposals_to_user` | `Proposals` — reassignment on leave |
| `save_partnership_update` | partnership list |
| `add_user_to_excludelist` / `remove_user_from_excludelist` | `excludeusers` list |
| `ensure_sharepoint_folder`, `upload_file_to_sharepoint`, `upload_file_to_sharepoint_folder` | document libraries |
| `add_or_update_user_in_excel`, `update_contact_in_onedrive_excel`, `upload_photo_to_onedrive` | OneDrive Excel + files |

**Three independent guards, because one is not enough:**

1. **Structural.** The production `sharepoint` connector class exposes **no write methods at all**.
   There is no `create`, `update`, `patch`, `upload` or `delete` to call — not from a service, not
   from a worker, not from a developer-panel tool. You cannot misuse an API that doesn't exist.
   Write capability lives in a *separate* `SharePointSandboxWriter` class, used only by tests and
   local development, hard-bound to the sandbox site at construction.
2. **Runtime — a site allowlist, checked by ID.** Every non-GET request is checked against an
   allowlist of writable SharePoint **site IDs**, which contains exactly one entry: the sandbox.
   Anything else raises `SharePointWriteForbidden` before a socket opens. The check compares the
   resolved site *ID*, not the URL string, so a mistyped or spoofed path cannot slip through.
   `connector_status.mode` displays `read_only` for every live site in both panels.
3. **CI.** A workflow step greps `backend/app/connectors/sharepoint/` for write verbs and fails the
   build on any match **outside** the sandbox-writer module. The guard cannot be quietly removed
   without an obvious, reviewable diff.

Where the legacy system wrote to SharePoint, 2.0 writes to Postgres instead. The
`useranalytics` list in particular becomes derived data — computed on demand from `proposals`,
which removes both the write *and* the 60-second sync entirely.

### 8.1.1 The sandbox site

**`https://hamdaz1.sharepoint.com/sites/sandbox` → list `sandboxlist`** — currently empty, and the
only SharePoint target Hamdaz 2.0 may write to.

| | |
|---|---|
| Site path | `/sites/sandbox` |
| List | `sandboxlist` |
| State | Empty — needs seeding (Phase 0) |
| Permitted | Read **and** write |

**Why this matters more than it looks.** Without it, the SharePoint connector's write path could
only ever be tested against fixtures — and fixtures cannot catch the things that actually break at
cut-over: Graph API throttling behaviour, `ETag` concurrency conflicts, column type coercion, and
lookup/person field quirks. The sandbox turns the Phase 8 SharePoint decision (§13 Q2) from a
gamble into something we can rehearse.

**Phase 0 seeding task.** Read the live `Proposals` list schema (read is permitted), mirror its
columns into `sandboxlist`, then populate with synthetic rows covering the awkward cases: person
fields, lookups, multi-value columns, empty and malformed dates, and unicode in titles.

> **A caution about `/sites/Test`.** It is misleadingly named and is **not** a sandbox. It holds
> **live** config and operational lists — `superusers`, `approvers`, `excludeusers`,
> `useranalytics`, quotes — that the production app reads on every boot. It is read-only like any
> other live site, and its site ID must never appear in the write allowlist. Only `/sites/sandbox`
> is writable.

Day-to-day development still runs primarily against **recorded fixtures** captured from read-only
syncs plus a seeded Postgres dataset — that keeps the test suite fast, offline and deterministic.
The sandbox is for the integration tests that genuinely need a real Graph endpoint.

### 8.2 Adjacent live side effects — a recommendation

C2 is about SharePoint, but the same reasoning applies to anything else with a real-world effect.
The legacy system sends live email through Graph from several paths — `send_quote_approval_email`,
`/api/send_email`, `/api/mails/send`, and the supplier-email sync. A development build that sends a
real quote request to a real supplier is a worse outcome than a stray SharePoint row.

**Recommendation:** outbound mail is disabled outside production by default. Non-production
environments capture messages to an outbox table that the developer panel renders, so the flow is
fully testable without anything leaving the building. Zoho writes get the same treatment.

This is my inference rather than something you specified — flagging it for a decision (§13 Q3).

---

## 9. Delivery phases

Assumes a **small team (2–3 engineers)**. Phases 4–7 are the parallelisable bulk; with 4–5
engineers the total compresses considerably.

### Phase 0 — Foundation *(≈2 weeks · weeks 1–2)*
Repo scaffolding, Docker Compose (postgres/redis/backend/worker/beat/frontend), FastAPI skeleton
with typed config, error handling, structured logging, correlation IDs. Alembic. Next.js shell with
Tailwind 4 + shadcn/ui. CI: ruff, mypy, pytest, eslint, tsc — **plus the SharePoint write-guard
check (§8.1)**, in from day one. **Seed the sandbox site** (§8.1.1): mirror the live `Proposals`
schema into `sandboxlist` and populate synthetic rows covering the awkward field types.
**Exit:** `docker compose up` runs the full stack; CI green; the sandbox holds usable test data.

### Phase 1 — Identity, teams & RBAC *(≈3 weeks · weeks 3–5)*
Azure AD OIDC → own session. `users`/`teams`/`roles`/`permissions`/`memberships` + permission
registry + enforcement dependency + automatic team-scope query filter. `audit_log` middleware.
**Exit:** a user signs in with Microsoft, lands in their team, and every mutating call is
authorized and audited.

### Phase 2 — Admin panel, labels & rules engine *(≈5 weeks · weeks 6–10)*
Screens in §5.1. The labels system (§5.3) with manual and auto-assignment. The rules engine core
(§5.2): decision-point registry, condition/action evaluator, versioning, publish/revert,
`rule_evaluations` logging, and the simulate-before-publish harness. Rules wired into the
decision points available this early — `user.label`, `notification.route`, `quote.approval_route`.
**Exit:** an admin creates a team, a custom role, a label, and a working rule set — and can
simulate it before publishing.

### Phase 3 — Developer panel & tool builder *(≈4 weeks · weeks 11–14)*
`job_runs`/`connector_status`/`tools`/`tool_runs`/`feature_flags`. Live SSE activity feed, job
control, connector health, error inbox, request tracing, metrics, **rule inspector**. Tool builder
with the three v1 implementation types, feature flags, safety rails (§6.3).
**Exit:** a developer creates a working tool, surfaces it as a UI action, and watches it execute in
the live feed — with no deploy.

### Phase 4 — Proposals & the assignment engine *(≈6 weeks · weeks 15–20)*
SharePoint **read-only** connector + delta sync → `proposals`. Proposal list/detail/reassignment.
**The `proposal.assign` policy engine (§5.4)** — eligibility, capacity by label, ratio and
weighted-least-loaded distribution, tie-breaks, fallback, manual override — with the preview
harness. Escalation rules. Shared projects, presence, notifications. Reports.
**Exit:** Pre-Sales can run a full day's proposal work in 2.0, and an admin can change how work is
distributed without a developer.

### Phase 5 — Commercial modules *(≈5 weeks · weeks 21–25)*
Zoho connector. Quotes, line items, rule-routed approval workflow, export to docx/xlsx. Vendors,
distributors, contacts, customers, partnerships/BD, competitor profiles.
**Exit:** quote lifecycle from creation through approval to export, end to end.

### Phase 6 — Communication & documents *(≈3 weeks · weeks 26–28)*
Graph mail client. Supplier email tracking and raw reply capture (no AI summary). Non-AI document
tools: PDF merge, technical proposal generation, quote export. Outbox gating per §8.2.
**Exit:** parity with the legacy mail and document modules, minus the deferred AI pieces.

### Phase 7 — Leave management *(≈2 weeks · weeks 29–30)*
Requests, availability and concurrency rules, holidays, rule-driven approval chain, handoff of
ongoing proposals (reusing the §5.4 engine), expiry job, admin leave console.
**Exit:** parity with the 14 legacy leave routes, with the policy now admin-configurable.

### Phase 8 — Data migration & cut-over *(≈3 weeks · weeks 31–33)*
Migration scripts: Cosmos (10 containers) → Postgres, SharePoint history → `proposals`, OneDrive
Excel → `contacts`/`customers`/`users`. Repeatable dry-runs with a reconciliation report. Parallel
run: 2.0 read-only alongside live 1.0, diffed daily. UAT per team. Cut-over runbook and rollback
plan. **The decision on SharePoint's future is made here** (§13 Q2): retire it, or lift C2 and add
a writeback connector for external consumers.
**Exit:** 2.0 is live; 1.0 is read-only, then decommissioned.

### Phase 9 — AI *(deferred · ≈5 weeks, after cut-over)*
pgvector and embeddings. AI assistant on the tool registry. Procurement analyzer. AI document
analysis and RFQ-vs-quote comparison. Supplier reply summarisation. The `pipeline` tool type.
Prompt manager with versioning. Per-team token budgets and spend visibility.

**Indicative total to cut-over: ~33 weeks / ~8 months** at 2–3 engineers, with AI following.

---

## 10. Non-functional requirements

| Area | Target |
|---|---|
| Performance | Dashboard p95 < 800 ms; list endpoints paginated and server-filtered, never full-table. Rule evaluation < 50 ms p95 |
| Scalability | Stateless API; horizontal scale safe (no global mutable state — root cause #3 fixed) |
| Reliability | Every external call retried with backoff + circuit breaker; jobs idempotent and replayable |
| Security | Azure AD SSO; least-privilege permissions; secrets in Key Vault; parameterised SQL only; CSRF on cookie auth; strict CSP; rate limits on tool endpoints |
| **Data safety** | **No write path to SharePoint exists (§8.1). Outbound mail gated outside production (§8.2)** |
| Auditability | Every mutating action in `audit_log`; every policy decision in `rule_evaluations` |
| Testing | ≥80% coverage on `services/`, and **100% on the rules evaluator** — it decides who gets work; integration tests against real Postgres via testcontainers; connector tests against recorded fixtures; Playwright E2E on critical paths |
| Observability | Structured JSON logs, OpenTelemetry traces, `/health` + `/ready`, all surfaced in the developer panel |
| Accessibility | WCAG 2.1 AA; keyboard navigation throughout |
| Browser | Evergreen Chrome/Edge/Safari; responsive to 1280 px, usable on tablet |

---

## 11. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | Legacy business rules are undocumented and exist only in code — especially `swp()` rotation, priority scoring and leave concurrency | High | Phase 2/4/7 each open with a written spec extracted from the legacy code and **confirmed with the team who uses it**. The rules engine then makes the policy visible and editable, so it can never silently re-hide in code |
| 2 | Cosmos → Postgres migration loses or mangles records | High | Repeatable dry-runs, row-count and checksum reconciliation, Cosmos kept read-only for 90 days post-cut-over |
| 3 | **An accidental write to a live SharePoint site corrupts production data** | **High** | Three independent guards (§8.1): no write methods on the production connector, an ID-checked site allowlist containing only the sandbox, and a CI check. Reviewed at every PR touching the connector |
| 4 | `/sites/Test` is mistaken for a sandbox because of its name | Medium | It is live and read-only like any other site (§8.1.1). The real sandbox is `/sites/sandbox`. Only that site ID sits in the write allowlist, so the mistake cannot become a write |
| 5 | A misconfigured assignment rule distributes work badly and nobody notices | Medium | Simulate-before-publish is mandatory; `rule_evaluations` makes every decision explainable; versioned policies revert in one click; a distribution-drift alert surfaces in the admin panel |
| 6 | The rules engine grows into a general-purpose programming language | Medium | Decision points and their fact schemas are a **fixed registry**, extended deliberately in code. Conditions and actions are declarative data, never executable code |
| 7 | The tool builder becomes a privilege-escalation hole | High | §6.3 rails: caller-permission execution, read-only SQL role, no arbitrary code, admin approval for write tools |
| 8 | Scope creep — 19 modules is a lot | High | Phases 4–7 are strictly parity-first. AI is already fenced into P9. New features only after cut-over |
| 9 | Live email sent from a development build reaches real suppliers | Medium | §8.2 outbox gating — pending your decision (§13 Q3) |
| 10 | Zoho Books connection is currently unauthorized in tooling | Medium | Re-authorize before Phase 5 |
| 11 | The two systems diverge during the Phase 8 parallel run | Medium | 2.0 stays read-only through the parallel run; writes move at a single cut-over moment. C2 makes this structurally safe — 2.0 *cannot* write to the shared source |

---

## 12. Immediate next steps

1. **Confirm this plan** — particularly §5.4, since the assignment rules are the piece most likely
   to need adjusting against how the team actually works.
2. **Answer the open questions in §13.**
3. Scaffold Phase 0: repo structure, Docker Compose, FastAPI + Next.js skeletons, CI **including the
   SharePoint write-guard**.
4. Audit SharePoint list consumers (§13 Q2) — this decides Phase 8's shape and is cheap now.
5. Workshop the label taxonomy (§5.3) with team leads: what categories actually exist, and what
   should each one's capacity be?

---

## 13. Open questions

1. **Teams** — exactly which teams exist, and what does each do day to day? The legacy code only
   implements Pre-Sales properly; BD is partial and Customer Success is a stub.
2. **SharePoint's future** — is anything *outside* this app reading or writing the `Proposals` list
   (Power Automate, Power BI, manual editing)? This decides whether SharePoint is retired at
   cut-over or needs a writeback connector.
3. **Outbound side effects** — do you want the §8.2 outbox gating for email and Zoho writes in
   non-production? I recommend yes.
4. **Labels** — what user categories do you actually use today, and what capacity should each
   carry? §5.4 uses New Joiner 0.4 / Part-time 0.5 / Team Lead 0.6 as placeholders.
5. **Assignment mode** — is `weighted_least_loaded` the right default, or do you want fixed ratios
   as the primary mode? (Both ship; this sets the default.)
6. **Users** — how many total, and expected growth? Sizes the infrastructure.
7. **Hosting** — stay on Azure App Service, or move to Azure Container Apps? Container Apps suits
   the API + worker + beat split better.
8. **Cut-over deadline** — is there a fixed date driving this? If so, scope gets prioritised against
   it rather than the other way around.
