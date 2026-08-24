# Hamdaz 2.0 — Frontend

Next.js 15 (App Router) · TypeScript strict · Tailwind 4.

```
tsc --noEmit ......... clean
eslint ............... clean
next build ........... 17 routes, 103 kB shared JS
```

---

## Getting started

```bash
cd infra && docker compose up          # everything, including the API
```

Or on its own, with the backend already running on :8000:

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000
```

| Command | |
|---|---|
| `npm run dev` | Development server |
| `npm run build` | Production build |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint |

---

## One CSS system

Root cause #6 in the audit: the legacy app loads **Bootstrap 5.3** and **Tailwind 2.2** from
CDN while also building **Tailwind 3.4** locally — three systems fighting over the same
elements. That is why the old UI is inconsistent.

Here there is one. Every colour, radius and shadow is a token in
[`app/globals.css`](app/globals.css); no component hardcodes a hex value. Dark mode
redefines **only the tokens**, so surfaces flip together and nothing is left painting light
text on a light ground.

| Token group | Meaning |
|---|---|
| `--color-accent` (petrol) | What we build and own |
| `--color-legacy` (brass) | Anything touching a legacy or external system — SharePoint-sourced rows carry it |
| `--color-good` / `warn` / `danger` | Semantic state, deliberately separate from the accent |
| `--color-ink*`, `--color-line*` | Neutrals with a faint cool bias, so greys read as chosen |

Fonts are self-hosted through `next/font` — no third-party request on load, no flash of
fallback text.

---

## Architecture

```
app/
├── login/                 Microsoft sign-in
├── (app)/                 authenticated shell
│   ├── layout.tsx         nav built from what THIS user may see
│   ├── dashboard/
│   ├── proposals/         list · detail · assign · timeline
│   ├── admin/
│   │   ├── teams/         teams, members, roles per member, labels per member
│   │   ├── roles/         the permission matrix
│   │   ├── labels/        category · skill · status
│   │   ├── rules/         decision points, rule builder, simulation, versions
│   │   └── assignment/    the §5.4 policy builder with live preview
│   └── developer/         overview · jobs · connectors · rule inspector · audit
components/
├── ui/                    primitives, all reading from the tokens
└── shell.tsx              sidebar + top bar
lib/
├── api.ts                 typed client; preserves RFC 7807 problem details
├── session.ts             getMe / can / teamsWith — mirrors the backend's Principal
├── types.ts               response shapes
└── format.ts              dates, durations, tones
```

### Server components by default

Pages fetch on the server with the session cookie forwarded, so the first paint already has
data. Only the genuinely interactive parts are client components: the policy builder, the
rule builder, the assign panel, and the small create/edit forms.

### Requests stay same-origin

`next.config.ts` rewrites `/api/v1/*` to the backend. The browser only ever talks to
`localhost:3000`, so the session cookie is first-party and CORS never enters the picture.

### Permissions

[`lib/session.ts`](lib/session.ts) mirrors the backend's `Principal.has`, including the rule
that matters most: **a team grant never satisfies an org-wide check.** It decides what to
*render* — the backend re-checks every call. Hiding a panel is a courtesy, not a control.

### Errors keep their detail

The backend returns RFC 7807 with a correlation ID and, on a 403, the exact permission that
was missing. [`lib/api.ts`](lib/api.ts) preserves all of it rather than collapsing to
"Request failed" — that detail is what makes a problem supportable.

---

## The two screens that matter most

**[Assignment policy](app/(app)/admin/assignment/)** — the §5.4 builder. Capacity per label,
eligibility ceilings, distribution mode, ratio targets. Structured as **edit → simulate →
publish**, and publish stays disabled until a simulation has run. The preview shows who would
get the next ten proposals, the spread across the team, and every exclusion with its reason.

Because the backend's engine is a pure function, the preview calls the *same code* the real
assignment does. It is not an approximation.

**[Rule builder](app/(app)/admin/rules/[id]/)** — conditions and actions are populated from
the decision point's declared facts and actions, so a rule that cannot be evaluated cannot be
authored. Simulation renders the full trace: every condition, whether it passed, and what the
actual value was. Unrecognised fact keys are called out explicitly, because a typo there is
the most common reason a rule "does not work".

---

## What is not built yet

| Area | State |
|---|---|
| Quotes, vendors, contacts | **Not built** — no backend endpoints yet either (P5) |
| Mail, documents | **Not built** (P6) |
| Leave | **Not built** — backend has models only (P7) |
| Zoho | Deferred to the next version, by decision |
| Live activity feed (SSE) | **Not built** — the developer panel's other screens are done |
| Tool builder | **Not built** |
| Pagination controls | List endpoints return `total`/`limit`/`offset`; the UI currently requests a large page and does not render pager controls |
| Optimistic updates | Mutations call `router.refresh()`. Correct, but a round-trip |
| Tests | **None.** No Playwright, no component tests |

### Not yet verified

**Nothing here has run against the live backend.** Type checking, linting and the production
build all pass, but every screen's data path is unexercised — no Postgres and no Docker on
the build machine. Expect the first real run to surface field-shape mismatches, which is
exactly what generating `lib/types.ts` from `/openapi.json` would prevent.
