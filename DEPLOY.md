# Deploying the backend to Azure App Service

Target: `hamdaz2-h4ajc0d0a3h9fwfa.centralus-01.azurewebsites.net`

## Current state: the site is up, the app is not on it

`GET /` returns Microsoft's "Azure App Service - Welcome" placeholder and `GET /health`
returns **404 from a stock gunicorn**. The web server is running; your code was never
deployed onto it. Nothing about this is visible from the outside — the hostname resolves and
answers, so the site looks healthy while serving none of your routes.

Two things cause that, and both need fixing.

## 1. Oryx had nothing to install

Azure's Python build system (Oryx) looks for `requirements.txt`. This project declares its
dependencies in `pyproject.toml` and installs them with `uv`, which Oryx does not read — so
it installed nothing and left gunicorn on its default app.

`backend/requirements.txt` now exists for exactly this. `pyproject.toml` stays the source of
truth; regenerate the manifest after any dependency change:

```powershell
uv pip compile pyproject.toml -o requirements.txt
```

Also set this app setting so Oryx actually runs a build on deploy:

```
SCM_DO_BUILD_DURING_DEPLOYMENT = true
```

## 2. There is no startup command

FastAPI is ASGI. Plain gunicorn is WSGI and cannot serve it, and `gunicorn` is not a project
dependency — so the usual `gunicorn -k uvicorn.workers.UvicornWorker` line will not work
here either. Use uvicorn directly.

**Configuration → General settings → Startup Command:**

```
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verified locally against this exact command with `ENVIRONMENT=production`:

```
{"status":"ok","app":"Hamdaz 2.0","environment":"production"}
```

Set the runtime to **Python 3.12 or newer**. This is not a preference: `app/workers/tasks.py`
uses PEP 695 generic syntax (`async def _run_tracked[T]`), which is a syntax error on 3.11.

## Application settings

`.env` is not deployed (`.gitignore` covers it, correctly). Every value must be set as an
App Service application setting.

| Setting | Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `DATABASE_URL` | the Azure Postgres URL, password URL-encoded (`@` → `%40`) |
| `JWT_SECRET` | a real secret — see below |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | from the app registration |
| `AZURE_REDIRECT_URI` | the deployed callback — see below |
| `SHAREPOINT_DOMAIN` | `hamdaz1.sharepoint.com` |
| `SHAREPOINT_READ_SITES` | `/sites/ProposalTeam,/sites/Test` |
| `SHAREPOINT_SANDBOX_WRITES_ENABLED` | `false` |
| `OUTBOUND_EMAIL_ENABLED` | `false` until you intend to email real suppliers |
| `CORS_ORIGINS` | the deployed frontend origin |
| `LOG_FORMAT` | `json` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |

**`JWT_SECRET` will stop the deploy if you forget it.** With `ENVIRONMENT=production` and the
placeholder value, the app raises on startup by design and App Service reports a container
that will not start. Generate one:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Changing it invalidates every existing session, which is the correct behaviour for a secret
that has been sitting in a local file.

## Three things that will bite after it boots

**Postgres firewall.** The App Service must be allowed to reach the database. Either enable
"Allow public access from Azure services" on the Postgres server, or add the App Service's
outbound IPs. Symptom: the app starts, `/health` passes, `/ready` fails — `/health` does not
touch the database and `/ready` does.

**The redirect URI must match exactly.** Set `AZURE_REDIRECT_URI` to the deployed callback and
add that same string to the app registration's Redirect URIs. Adding is additive and will not
disturb the existing local entry. A mismatch gives `AADSTS50011`.

**Graph application permission.** The SharePoint sync authenticates as the app, not as a user,
so it needs `Sites.Read.All` under *Application* permissions with admin consent. Already
consented on this registration — but if you deploy against a different one, this is the
failure that shows up as a Graph 403 while sign-in still works.

## Why this is worth doing

Measured from this machine:

```
Postgres    hamdaz.postgres.database.azure.com   20.29.80.34    median RTT = 310ms
App Service centralus-01                         20.118.48.67   median RTT = 326ms
```

Both are roughly a third of a second away, which means the database is in the US, not near
you — and almost certainly beside the App Service. Every database round trip currently
crosses an ocean, and an authenticated request needs several of them, so page loads run to
whole seconds. Running the backend in Central US collapses that leg from ~310 ms to
single-digit milliseconds.

That is the fix. The query-count work in `principal_service.py` (11 statements down to 4) and
the parallelised dashboard fetches help everywhere, but they were only ever removing *extra*
round trips — they cannot make a remaining one cheaper.

## Pointing the frontend at it

```
BACKEND_URL = https://hamdaz2-h4ajc0d0a3h9fwfa.centralus-01.azurewebsites.net
```

`next.config.ts` proxies `/api/v1/*` there, and `lib/api.ts` uses the same value for
server-component fetches, so one variable moves both.

Note where the remaining latency then sits: a frontend still running on your laptop reaches
the backend across the same ~326 ms. It will be far faster than today, because the many
database round trips per request become local to Azure — but hosting the frontend in the
same region removes that last hop too.
