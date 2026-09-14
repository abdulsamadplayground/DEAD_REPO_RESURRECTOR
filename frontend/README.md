# Resurrector Dashboard (frontend)

A modern, dynamic operations console for the **Dead Repo Resurrector**. It
replaces the old JSON/table static page with a live view of every tracked
repository, the Analyst → Engineer → Communicator agent pipeline, and per-repo
issue / PR / activity detail.

Built to be hosted as static assets on **S3 + CloudFront** (per
`aws-constraints.md`) — it is a pure client-side bundle that fetches the
read-only `GET /state` endpoint (`dashboard_lambda`). No server, no build step
at runtime.

## Stack (current as of 2026)

| Concern         | Choice                                    |
| --------------- | ----------------------------------------- |
| Framework       | React 19 + TypeScript                     |
| Build tool      | Vite 6                                     |
| Styling         | Tailwind CSS v4 (`@tailwindcss/vite`)     |
| Server state    | TanStack Query v5 (30s auto-refresh)      |
| Animation       | Motion (Framer Motion successor)          |
| Charts          | Recharts 3                                 |
| Icons           | lucide-react                              |

## Develop

Node is installed locally at `~/.local/node`. Put it on `PATH` first:

```sh
export PATH="$HOME/.local/node/bin:$PATH"

cd frontend
npm install        # first time only
npm run dev        # http://localhost:5173
npm run build      # type-check + production bundle -> frontend/dist
npm run preview    # serve the built bundle locally
```

## Pointing the dashboard at real data

The app resolves its API URL at runtime (no rebuild needed), first match wins —
identical to the legacy `src/dashboard/index.html`:

1. `?api=<url>` query parameter
2. `window.DASHBOARD_API_URL` global (inject a `<script>` at deploy time)
3. `<meta name="dashboard-api" content="<url>">` in `index.html`
4. relative default `/state`

Deploy step (Task 13) should inject the API Gateway invoke URL — e.g. the
`DashboardApiUrl` stack output — via option 2 or 3.

If the endpoint is unreachable, the UI renders clearly-labeled **demo data** so
the page never dead-ends. Force demo data with `?demo=1`.

## What it shows

- **KPI cards** — repos in the pipeline, PRs awaiting review, fixes merged,
  acceptance rate.
- **Agent pipeline** — repos bucketed by the stage their status implies, with a
  live "working" indicator on the Engineer while a fix is in progress.
- **Status distribution** — donut of every lifecycle state.
- **Repository table** — searchable + status-filterable, links to real PRs.
- **Detail drawer** — issue, per-repo agent activity timeline
  (Analyst → Engineer → Communicator), PR + maintainer engagement, notes.

## Deploy to S3/CloudFront

```sh
npm run build
aws s3 sync dist/ s3://<your-dashboard-bucket>/ --delete
# then invalidate the CloudFront distribution if used
```

Assets use relative paths (`base: "./"`), so the bundle works under any bucket
prefix or distribution path.
```
