# Running Hamdaz 2.0 — no Docker

Windows, PowerShell. Everything runs directly on the machine.

> **Nothing here has been executed yet.** The code type-checks, lints, tests and builds, but
> no part of it has run against a real database or a real Azure tenant. The last section
> lists where the risk actually sits.

---

## What you actually need

| | Needed for | Required? |
|---|---|---|
| **Python 3.12+** | Backend | Yes — you have 3.14 |
| **Node 20+** | Frontend | Yes — you have 24 |
| **Postgres 16** | Everything | Yes |
| **Redis 7** | Background jobs only | **No** — skip it to start |

**Redis is optional.** The API never imports Celery, and `/ready` only checks the database.
Without Redis you get the whole application; you just do not get the *scheduled* SharePoint
sync, label expiry and escalation jobs. Add it later.

You can still pull live SharePoint data without Redis — trigger the ingest by hand:

```powershell
curl.exe -X POST http://localhost:8000/api/v1/developer/connectors/sharepoint/sync
```

It runs the same read-only ingest in-process and returns what it did. Needs the
`dev.jobs.manage` scope, and returns the real Graph error if the app registration is not
set up (see below).

---

## Step 0 — there is no migration yet

`backend/alembic/versions/` is **empty**. Alembic needs a live Postgres to generate the first
migration from the models, which is why it does not exist yet.

`alembic upgrade head` on an empty versions directory **succeeds and creates no tables**. It
is easy to skip this and then wonder why every page 500s. Step 3 does it.

---

## Step 1 — Postgres

Install:

```powershell
winget install PostgreSQL.PostgreSQL.16
```

The installer asks for a password for the `postgres` superuser — remember it.

`psql` is not on PATH by default. Either add `C:\Program Files\PostgreSQL\16\bin` to PATH, or
use the full path below.

Create the database and user:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres
```

At the `postgres=#` prompt:

```sql
CREATE USER hamdaz WITH PASSWORD 'hamdaz';
CREATE DATABASE hamdaz OWNER hamdaz;
\q
```

Check it is reachable:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U hamdaz -d hamdaz -c "SELECT version();"
```

---

## Step 2 — Backend configuration

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend
Copy-Item .env.example .env
```

Generate a session secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Open `backend\.env` and set these. Everything else in the file can stay as it is:

```bash
DATABASE_URL=postgresql+psycopg://hamdaz:hamdaz@localhost:5432/hamdaz
JWT_SECRET=<the value you just generated>
LOG_FORMAT=console

AZURE_TENANT_ID=<your tenant id>
AZURE_CLIENT_ID=<your app registration client id>
AZURE_CLIENT_SECRET=<client secret VALUE, not the secret id>
AZURE_REDIRECT_URI=http://localhost:3000/api/v1/auth/callback
```

### The redirect URI matters

In the Azure portal, under your app registration → **Authentication** → **Web** → Redirect
URIs, add exactly:

```
http://localhost:3000/api/v1/auth/callback
```

Port **3000**, not 8000. Next proxies `/api/v1/*` to the backend so the browser stays on one
origin, which keeps the session cookie first-party. The value in Azure and the value in
`.env` must match character for character.

---

## Step 3 — Create the schema

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend

uv sync --extra dev

# Generate the first migration from the models, then apply it.
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

Confirm the tables exist before moving on:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U hamdaz -d hamdaz -c "\dt"
```

You should see roughly 25 tables — `users`, `teams`, `roles`, `permissions`, `memberships`,
`labels`, `rule_sets`, `proposals`, `audit_log` and so on. If you see nothing, the
`revision --autogenerate` step did not run.

---

## Step 4 — Seed and create the first admin

```powershell
# Permission registry, system roles, default labels. Idempotent.
uv run python -m app.cli seed

# Creates the Pre-Sales team and makes you super admin.
uv run python -m app.cli bootstrap you@hamdaz.com
```

Use your **real Hamdaz email** — it is matched against the Microsoft account you sign in
with. `bootstrap` refuses to run twice; after this, add people through the admin panel.

---

## Step 5 — Run it

Two terminals.

**Terminal 1 — backend**

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend
uv run uvicorn app.main:app --reload --port 8000
```

Wait for `app.started`.

**Terminal 2 — frontend**

```powershell
cd C:\Users\ansha\hamdaz-2.0\frontend
npm install
npm run dev
```

| | |
|---|---|
| **Application** | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness | http://localhost:8000/ready |

`/ready` should return `{"status":"ready","checks":{"database":"ok"}}`. If it says
`degraded`, Postgres is not reachable — fix that before going further.

---

## Verify without any of the above

These need no database and no configuration. Worth running first:

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend
uv run python -m app.cli check                        # registries agree with themselves
uv run python scripts/check_sharepoint_readonly.py    # constraint C2 (guard #3)
uv run pytest                                         # 222 tests
uv run ruff check .
uv run mypy app
```

```powershell
cd C:\Users\ansha\hamdaz-2.0\frontend
npm run typecheck
npm run lint
npm run build
```

---

## First sign-in

1. Open http://localhost:3000 → redirected to `/login`.
2. **Continue with Microsoft** → Azure → back to the app.
3. Having run `bootstrap` with your own email, you land on the dashboard as super admin.
4. Anyone else signing in gets an account with **no team** and a "Waiting for access" banner.
   That is deliberate: signing in never grants access. Add them under
   **Admin → Teams → *team* → Members**.

---

## A first walkthrough

1. **Admin → Labels** — the defaults are already seeded (`new-joiner`, `senior`, `part-time`,
   `excluded-from-rotation`, …).
2. **Admin → Teams → Pre-Sales → Members** — add two or three people. Give one `new-joiner`
   and another `senior`.
3. **Admin → Assignment policy** → **Simulate**. At equal open-task counts the new joiner
   should rank *below* the senior, with the reasoning spelled out per row. Publish when it
   looks right.
4. **Proposals** → create one → **Run assignment policy**, and read the explanation.
5. **Developer → Rule inspector** — the decision you just made, with the facts the engine saw.

---

## Check the constraints hold

**Developer → Overview** should show:

- **SharePoint: read only** — live sites are never written to.
- **Outbound email: captured, not sent** — nothing leaves the building outside production.

If either says otherwise on your machine, stop and check `backend\.env`.

---

## Adding Redis later (optional)

Only needed for the scheduled SharePoint sync, label expiry and escalation jobs. Redis has no
good native Windows build; two workable options:

**Memurai** (Redis-compatible Windows service):

```powershell
winget install Memurai.MemuraiDeveloper
```

**Or WSL2:**

```powershell
wsl --install
wsl -e bash -c "sudo apt update && sudo apt install -y redis-server && sudo service redis-server start"
```

Either way it listens on `localhost:6379`, which is already the default in `.env`.

Then, in two more terminals:

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend
uv run celery -A app.workers.celery_app.celery_app worker --loglevel=info --pool=solo
```

```powershell
cd C:\Users\ansha\hamdaz-2.0\backend
uv run celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

`--pool=solo` is required on Windows — Celery's default prefork pool does not work there.

Run **exactly one** beat instance. Two would double every scheduled job, which is precisely
the bug the legacy in-process threads had.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Tables missing after `upgrade head` | The migration was never generated. Re-run the `revision --autogenerate` step. |
| `/ready` returns 503 | Postgres unreachable. Check `DATABASE_URL`, and that the Postgres service is running (`Get-Service postgresql*`). |
| `password authentication failed for user "hamdaz"` | The `CREATE USER` step did not run, or the password differs from `DATABASE_URL`. |
| `Microsoft sign-in is not configured` | `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` are still blank in `.env`. |
| `AADSTS50011: redirect URI mismatch` | The Azure Redirect URI must match `AZURE_REDIRECT_URI` exactly, including the port. |
| Sign-in works but no proposals ever appear | The scheduled sync needs Redis + a Celery worker. Trigger it by hand (see "What you actually need") to see the real error. |
| SharePoint connector unhealthy, `AADSTS7000215` | Wrong `AZURE_CLIENT_SECRET`. |
| SharePoint sync fails with a Graph 403 | The app registration is missing the **application** permission `Sites.Read.All` with admin consent. Delegated permissions are not enough — the sync runs with no user. |
| Proposals sync but customer/assignee are blank | The `Proposals` list column names changed. `FIELD_MAP` in `app/services/sharepoint_sync.py` is asserted against the live schema by `tests/test_sharepoint_mapping.py`. |
| `Sign-in did not start here, or it took too long` | The PKCE cookies were lost. Start from `/login`; do not paste a callback URL directly. |
| Signed in, but everything is empty | Correct for a user with no team. Add them to one. |
| `jwt_secret must be set in production` | `ENVIRONMENT=production` with the placeholder secret. Refusing to start is intentional. |
| Frontend 401s everywhere | Backend not running on :8000, or `BACKEND_URL` overridden. |
| `uv: command not found` | `pip install uv` |
| Celery worker exits immediately on Windows | Missing `--pool=solo`. |

---

## What to expect on the first real run

Being straight about where the risk is:

- **Service-layer SQL is unexercised.** All 222 backend tests run with the database
  dependency overridden. The queries themselves have never executed. Expect the first
  failures here.
- **The frontend has never spoken to the backend.** `lib/types.ts` is hand-written, so field
  names may not match. Once the API is up, replace it at the root:

  ```powershell
  cd C:\Users\ansha\hamdaz-2.0\frontend
  npx openapi-typescript http://localhost:8000/openapi.json -o lib/api-types.ts
  ```

- **The OIDC flow has never run against a real tenant.** It is implemented with PKCE and full
  ID-token verification, but the first attempt is the first test.
- **The SharePoint sync needs a Graph token** that nothing supplies yet. Its task reports
  `skipped: no access token` until that is wired up — which is the correct behaviour, not a
  failure.

The pure engines are the exception. The rules evaluator and the assignment logic have 96
tests between them and depend on nothing external, so they will behave exactly as the tests
say regardless of what the infrastructure does.
