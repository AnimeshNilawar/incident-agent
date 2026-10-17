# Local Deployment Preparation (Cloud Run + Cloud Tasks)

This document prepares a future Cloud Run API/worker deployment with Cloud
Tasks. It is **local-only**: it describes the container image, the health
checks, the deployment layout, and what a real deployment would still need.
Nothing here has been provisioned or deployed, and no cloud API has been
called.

Related documents:

- [`cloud-tasks-deployment.md`](cloud-tasks-deployment.md) — queue
  assumptions, OIDC audience, IAM, retry semantics.
- [`database-and-migrations.md`](database-and-migrations.md) — schema
  isolation and how migrations are applied.
- [`recovery-and-worker-lifecycle.md`](recovery-and-worker-lifecycle.md) —
  claim/lease/fencing model and recovery.

## Deployment layout: two services, one image

**Recommendation: run the API and the worker as two Cloud Run services built
from the same image, selected by `SERVICE_ROLE`.**

| | One service (all routes) | Two services (`api` + `worker`) |
|---|---|---|
| Cloud Run IAM | Cannot mix: ingress authentication is **service-wide**, so the service is either public (worker protected only by in-app OIDC) or authenticated (Grafana/incident API unreachable). | Worker service is authenticated-only (`roles/run.invoker`); API service stays reachable by Grafana. Correct by construction. |
| Attack surface | Every route exposed on one host, including the recovery endpoint. | Worker service mounts only two OIDC-protected routes. |
| Scaling | One shared scaling curve for public traffic and background tasks. | Worker can scale to zero between tasks; API stays warm. |
| Least privilege | One identity needs every permission. | The invoker binding exists only on the worker service. |
| Operational cost | One deployment. | Two deployments of the same, identically-tagged image. |

The deciding factor is that **Cloud Run authenticates at the service level,
not per path**. The worker and recovery endpoints must keep their OIDC gate
(`verify_cloud_tasks_request` / `verify_recovery_request`), and the Phase 2A
design explicitly expects gateway authentication *plus* in-app verification as
defence in depth. A single public service would lose the gateway layer; a
single authenticated service would break Grafana. Splitting by role satisfies
both, so it is the recommended layout.

The split is implemented as route selection, not a code fork:

- `SERVICE_ROLE=all` (default) — every route, one process. Local development
  and the existing test suite are unchanged.
- `SERVICE_ROLE=api` — `/api/v1/incidents`, `/api/v1/webhooks/*`.
- `SERVICE_ROLE=worker` — `/api/v1/worker/*`, `/api/v1/internal/*`.
- `/health/live`, `/health/ready`, and `/` are mounted in every role.

An unknown `SERVICE_ROLE` fails at startup rather than silently starting with
the wrong route surface. See `app/main.py` (`create_app`) and
`tests/test_service_role.py`.

> The worker role registers no public routes, but the worker endpoints still
> require their OIDC token — the role split is defence in depth, not a
> replacement for authentication.

## Ingress and the public API boundary

How Grafana reaches the API service is a security decision that the code does
not make for you. Two supported shapes:

| Ingress | How Grafana reaches it | Trade-offs |
|---|---|---|
| **Public** (`--allow-unauthenticated`) | Grafana calls the public HTTPS URL directly. | Simplest, works from any Grafana (including Grafana Cloud or an on-prem instance with no VPC path). The whole service is internet-reachable, but both the incident API and the webhook require their own HTTP Basic credentials. |
| **Restricted** (`--ingress=internal` / `internal-and-cloud-load-balancing`, or an auth-required service with a Serverless NEG + load balancer, Cloud VPN/Interconnect, or a VPC connector) | Grafana must share a network path: an internal load balancer, Serverless VPC Access, or IAM-authenticated calls with a minted token. | Removes the incident API and webhook from the public internet, but only works if the actual Grafana instance *has* that path. Do **not** assume a private Cloud Run service is reachable by an external/grafana.com-hosted Grafana — it is not. |

The webhook is protected by HTTP Basic (`GRAFANA_WEBHOOK_USERNAME` /
`GRAFANA_WEBHOOK_PASSWORD`), and it **fails closed**: if either credential is
empty, every request is answered `503`; a missing header is `401`; a wrong
credential is `401`. Error bodies never contain the configured password. See
`tests/test_grafana_webhook.py`.

The **incident-management API** (`GET`/`POST /api/v1/incidents`) is protected
by a *separate* HTTP Basic credential pair —
`INCIDENT_API_USERNAME` / `INCIDENT_API_PASSWORD` — so the Grafana webhook
secret is never reusable as a general API credential (and vice versa). It
follows the same fail-closed rules (empty or partial config → `503`, missing
header → `401`, wrong credential → `401`), compares values in constant time,
and never echoes credentials in an error body. Authentication is enforced
*before* incident lookup, so an unauthenticated caller cannot probe which
incident IDs exist. See `app/api/incidents.py` and
`tests/test_incidents_auth.py`.

> **Ingress recommendation.** With both the incident API and the webhook
> authenticated, the API service can be exposed publicly
> (`--allow-unauthenticated`) without granting unauthenticated read/write
> access. Restricted ingress (`--ingress=internal` /
> `internal-and-cloud-load-balancing`, an internal load balancer, or a VPC
> connector) remains the stronger defence-in-depth choice **where the actual
> Grafana instance has that network path** — but do not assume an external /
> grafana.com-hosted Grafana can reach a private Cloud Run service; it cannot.
> Either way, treat the incident API credential as a real secret and rotate it
> via Secret Manager.

This section documents the Phase 2C change: the incident API previously had no
authentication dependency; it now requires its own HTTP Basic credential.

## Container image

`Dockerfile` (single stage, `python:3.14-slim`, matching the 3.13/3.14 CI
matrix) and `.dockerignore` are new in this phase.

- **ASGI server:** `uvicorn app.main:app` (uvicorn is already pinned). No
  `gunicorn` is added, and no process manager wrapper is used.
- **Port:** listens on `${PORT}` (Cloud Run supplies it), defaulting to
  `8080` for `docker run`.
- **Graceful shutdown:** the entrypoint is
  `exec uvicorn ...` under `sh -c`, so uvicorn is PID 1 and receives
  `SIGTERM`.
- **Non-root:** a dedicated `appuser` (uid 10001) owns `/app`; the container
  runs as that user.
- **No secrets or local artefacts:** `.dockerignore` excludes `.env*`,
  `.venv`, `.git`, `*.db`, caches, tests, and docs. `docker run` smoke tests
  confirmed no `.env` is present in the image and that the only `.db` file is
  the one created at runtime by the configured `DATABASE_URL`.
- **Reproducible dependencies:** `requirements.txt` pins exact versions and
  is copied before the application code so the dependency layer is cached.
  Every dependency ships a manylinux wheel, so **no compiler is installed**.
- **No migrations at startup:** the image contains `alembic.ini` and
  `migrations/` so a future migration job can reuse it, but nothing runs
  `alembic upgrade` or `create_all` automatically for PostgreSQL. Convenience
  `create_all` remains gated to local SQLite only.
- **SQLite local workflow still works:** the image sets no `ENVIRONMENT` or
  `DATABASE_URL`; running with `-e ENVIRONMENT=local` keeps the existing
  SQLite defaults.

Build and run locally (no registry, no cloud):

```powershell
docker build -t incident-agent:local .

# API + worker in one process (local behaviour)
docker run --rm -e ENVIRONMENT=local `
  -e DATABASE_URL=sqlite:////tmp/incident_agent.db -p 8080:8080 incident-agent:local

# Worker-only process
docker run --rm -e ENVIRONMENT=local -e SERVICE_ROLE=worker `
  -e DATABASE_URL=sqlite:////tmp/incident_agent.db -p 8080:8080 incident-agent:local
```

## Health endpoints

| Endpoint | Purpose | Checks |
|---|---|---|
| `GET /health/live` | Liveness. Process is up and serving HTTP. | **Nothing.** Uses no dependency, so a slow database cannot cause a restart loop. |
| `GET /health/ready` | Readiness. Can this instance take traffic? | **Database connectivity only** — it opens a connection and runs `SELECT 1`. Returns `503` when unreachable. |

`/health/ready` **does** verify the database: `database_is_reachable()` in
`app/db/session.py` actually connects and queries. It does **not** verify
Cloud Tasks, Google token verification, or Grafana credentials — those are
outbound/configuration dependencies that a probe must not call on every
request, and a healthy response must not be read as "Cloud Tasks works".
Readiness responses contain only `{"status", "database"}`; the URL,
credentials, and any traceback are never returned or logged (only the
exception *type* is logged, at `WARNING`).

Wire readiness to `GET /health/ready` and liveness to `GET /health/live`.
A non-2xx readiness response tells Cloud Run to stop routing traffic; it does
not restart the instance.

Suggested Cloud Run probe mapping:

| Cloud Run probe | Path | Why |
|---|---|---|
| Startup | `/health/live` | No dependency, so a slow database cannot prevent the container from starting. |
| Liveness | `/health/live` | No dependency, so a transient database blip cannot trigger a restart loop. |
| Readiness | `/health/ready` | Stops new traffic while the database is unreachable. |

Cloud Run readiness probes are currently a **Preview** feature; startup and
liveness probes are GA. The endpoints themselves work under any orchestrator
that supports HTTP readiness checks.

## Startup behaviour with missing configuration

Configuration is validated when `Settings` is constructed, i.e. at import
time, so a misconfigured container fails to start (a Cloud Run revision that
never becomes healthy) instead of serving traffic with wrong assumptions:

| Condition | Behaviour |
|---|---|
| Non-local `ENVIRONMENT` + SQLite or empty `DATABASE_URL` | Process fails to start. |
| Non-local `ENVIRONMENT` + PostgreSQL URL | Starts; migrations are a separate step. |
| `SERVICE_ROLE` not in `all`/`api`/`worker` | Process fails to start with a clear message. |
| Missing Cloud Tasks config | Process starts; only *dispatch* fails fast (the job stays `pending`, missing variable named in `last_error`). |
| Missing worker/recovery auth config | Process starts; those endpoints answer `503` (fail closed) instead of trusting a caller. |
| Missing incident API credentials | Process starts; the incident API answers `503` (fail closed) instead of serving unauthenticated requests. |

## Authentication boundaries by role

| Role | Incident API (HTTP Basic) | Grafana (HTTP Basic) | Worker (OIDC) | Recovery (OIDC, separate audience) |
|---|---|---|---|---|
| `all` | yes, fail-closed | yes | yes | yes |
| `api` | yes, fail-closed | yes | not mounted (404) | not mounted (404) |
| `worker` | not mounted (404) | not mounted (404) | yes, fail-closed | yes, fail-closed |

Every mountable route is authenticated: the `api` service serves the incident
API and the Grafana webhook (two *distinct* HTTP Basic secrets), and the
`worker` service serves only the OIDC-protected worker and recovery routes.
There is no unauthenticated incident surface.

Recovery uses a different audience and service account from the worker, so a
task-delivery token cannot trigger recovery (see
`cloud-tasks-deployment.md`).

## Degrading safely

- **Cloud Tasks unavailable at dispatch:** the job is left `pending` with
  `last_error` set — never marked queued. Transient failures log at `WARNING`,
  permanent ones (`PermissionDenied`, `InvalidArgument`, missing
  configuration) at `ERROR` for alerting. See Phase 2A notes in
  `cloud-tasks-deployment.md`.
- **Database unavailable:** `/health/ready` returns `503`; the platform stops
  routing to the instance. Requests that need the database surface a `5xx`
  rather than corrupting state.
- **Cloud Tasks unavailable at delivery time:** the worker still processes
  a delivery and Cloud Tasks retries non-2xx responses; the application's
  `max_attempts` remains authoritative.

## CI

`.github/workflows/ci.yml` has two jobs:

1. `checks` (existing) — Ruff, the SQLite/unit suite, `alembic upgrade` +
   `alembic check` against an ephemeral `postgres:16` service, and the
   PostgreSQL integration suite. No cloud credentials.
2. `container` (new in Phase 2B.1) — builds the image with `docker build`
   and smoke-tests it with a local SQLite database: `/health/live` and
   `/health/ready` must return `200`; the incident API must answer `401`
   without credentials and `200` with the configured basic-auth credential;
   and under `SERVICE_ROLE=worker` the public route must be `404` while the
   worker route stays `401`. It never pushes an image and uses no registry or
   cloud credentials.

Building the container in CI requires only a Docker-capable runner
(`ubuntu-latest` provides it); no secrets are needed. **The `container` job
has not yet run on a real GitHub runner** — it is validated locally (see the
completion report) but its first real CI result is still pending.

## Deployment checklist (A–I)

An ordered, implementation-backed checklist for taking this repository from
`main` to a working Cloud Run + Cloud Tasks + Supabase deployment. Nothing
here has been performed or verified against Google Cloud; the Cloud Tasks API
and every IAM binding remain **unexercised**.

**Legend**

- **LOCAL** — runs on a workstation or CI, contacts no hosted resource.
- **CLOUD** — creates or modifies a Google Cloud / Supabase resource, may
  incur cost, or changes a live service. Requires deliberate review.
- Placeholders — `<PROJECT_ID>`, `<REGION>`, `<AR_REPO>`, `<API_SERVICE>`,
  `<WORKER_SERVICE>`, `<QUEUE>`, `<API_RUNTIME_SA>`, `<TASKS_SA>`,
  `<RECOVERY_SA>`, `<SUPABASE_HOST>`, `<TAG>`. None are real values.
- `gcloud` flags are representative; confirm them against current `gcloud`
  documentation before running (the CLI is not a stable interface).
- The repository *defaults* `CLOUD_TASKS_LOCATION=asia-south1` and
  `CLOUD_TASKS_QUEUE=incident-investigations` (`app/config.py`, mirrored in
  `.env.example`). They are **non-empty defaults**, so dispatch does *not*
  fail-fast if they are left unset — set them explicitly to avoid silently
  targeting the wrong region/queue.

### A. GitHub CI validation — LOCAL (CI)

- **Prerequisites:** the uncommitted work pushed to a branch; GitHub Actions
  enabled; a Docker-capable runner (`ubuntu-latest`).
- **Action:** open a pull request (or `workflow_dispatch`). The `checks` job
  runs Ruff, the SQLite suite, `alembic upgrade head` + `alembic check`
  against an ephemeral `postgres:16` service, and the PostgreSQL integration
  suite. The `container` job builds the image and runs the two smoke tests.
- **Expected verification:** both jobs pass; `container` logs
  `{"status":"alive"}`, `{"status":"ready",...}`, and `incidents=404
  worker=401`.
- **Resource / cost / live change:** creates no cloud resource; consumes
  Actions minutes. No secrets required.
- **Blocker:** the `container` job has **never run on a real runner** (Phase
  2B.1 only validated it locally). Treat A as unverified until a real run is
  observed.

### B. Dedicated Supabase connection and migration — CLOUD (DDL)

- **Prerequisites:** a Supabase database dedicated to the agent (or at least
  isolated by schema); the transaction-mode pooler host; a **migration role**
  with `USAGE` + `CREATE` on `incident_agent`.
- **Required values:** `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@<SUPABASE_HOST>:6543/postgres?sslmode=require`,
  `DATABASE_SCHEMA=incident_agent` (fixed — any other value is rejected at
  startup).
- **Verify the target before migrating:** run the non-connecting inspector
  `python -m app.db.target` to print the target's host, port, database,
  username, and TLS mode *without* the password; confirm it is the intended
  Supabase pooler (`:6543`, `sslmode=require`). There is no automatic
  production guard on the `alembic` command itself — the guard
  (`PRODUCTION_MARKERS`) applies only to the *integration test* fixture, so an
  operator-set `DATABASE_URL` is trusted. Setting it in a shell (not `.env`)
  avoids committing credentials.
- **Action (LOCAL command, CLOUD effect):**

  ```powershell
  $env:DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@<SUPABASE_HOST>:6543/postgres?sslmode=require"
  .\.venv\Scripts\python.exe -m app.db.target      # confirm target, no password printed
  .\.venv\Scripts\python.exe -m alembic upgrade head
  .\.venv\Scripts\python.exe -m alembic current
  .\.venv\Scripts\python.exe -m alembic check
  ```

- **Expected verification:** `alembic current` reports
  `794b420b8bfc (head)`; `alembic check` reports no new upgrade operations;
  the `incident_agent` schema contains `incidents`, `investigation_jobs`, and
  `alembic_version`, and **nothing** is created in `public`.
- **Resource / cost / live change:** creates schema + tables (DDL) in the
  existing database. No new cloud resource; Supabase plan cost.
- **Why not at startup:** `app/main.py` only auto-creates tables for local
  SQLite; PostgreSQL is migrated as an explicit step (see
  `database-and-migrations.md`).
- **Runtime role:** after migrating, the service's runtime role needs only
  `USAGE` on `incident_agent` plus `SELECT/INSERT/UPDATE/DELETE` (see the SQL
  in `.env.example`). The pooler prerequisites (`prepare_threshold=None`,
  `pool_pre_ping`, `pool_recycle`) are already implemented in
  `app/db/session.py`.

### C. Artifact Registry and image publication — CLOUD

- **Prerequisites:** Artifact Registry API enabled; a repository in `<REGION>`.
- **Action:**

  ```bash
  gcloud artifacts repositories create <AR_REPO> \
    --repository-format=docker --location=<REGION>
  docker build -t <REGION>-docker.pkg.dev/<PROJECT_ID>/<AR_REPO>/incident-agent:<TAG> .
  gcloud auth configure-docker <REGION>-docker.pkg.dev
  docker push <REGION>-docker.pkg.dev/<PROJECT_ID>/<AR_REPO>/incident-agent:<TAG>
  ```

- **Expected verification:** the tagged image is listed by
  `gcloud artifacts docker images list`.
- **Resource / cost / live change:** creates a registry and stores the image
  (storage cost). No service change.
- **LOCAL note:** `docker build` + the Phase 2B.1 smoke tests can be rerun
  locally before pushing; nothing here uploads without the explicit `push`.

### D. Cloud Run API service — CLOUD

- **Prerequisites:** image in Artifact Registry (C); Cloud Run API enabled;
  Supabase migrated (B); secrets created (G).
- **Required values:** `SERVICE_ROLE=api`, `ENVIRONMENT=production`
  (non-local, so a PostgreSQL URL is mandatory), `DATABASE_URL`,
  `INCIDENT_API_USERNAME`, `INCIDENT_API_PASSWORD`,
  `GRAFANA_WEBHOOK_USERNAME`, `GRAFANA_WEBHOOK_PASSWORD`, `GCP_PROJECT_ID`,
  `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `INVESTIGATION_WORKER_URL`,
  `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`, `INVESTIGATION_WORKER_AUDIENCE`.
- **Action (representative):**

  ```bash
  gcloud run deploy <API_SERVICE> \
    --image=<REGION>-docker.pkg.dev/<PROJECT_ID>/<AR_REPO>/incident-agent:<TAG> \
    --region=<REGION> \
    --set-env-vars=SERVICE_ROLE=api,ENVIRONMENT=production,GCP_PROJECT_ID=<PROJECT_ID>,... \
    --set-secrets=DATABASE_URL=...,GRAFANA_WEBHOOK_PASSWORD=...,INCIDENT_API_PASSWORD=... \
    --allow-unauthenticated     # the incident API and webhook are authenticated in-app
  ```

- **Expected verification:** `GET https://<API_SERVICE URL>/health/live` →
  `{"status":"alive"}`; `/health/ready` → `{"status":"ready",...}`;
  `GET /api/v1/incidents` without credentials → `401`; with the configured
  credential → `200`.
- **Resource / cost / live change:** creates a Cloud Run service (compute
  cost); with `--allow-unauthenticated`, changes public exposure — which is
  safe only because the incident API and webhook now authenticate.
- **Decision:** public ingress is acceptable with authenticated routes;
  restricted ingress is stronger defence-in-depth where Grafana has a network
  path (see "Ingress and the public API boundary").

### E. Cloud Run worker service — CLOUD

- **Prerequisites:** image in Artifact Registry (C); Supabase migrated (B);
  `<TASKS_SA>` exists (F).
- **Required values:** `SERVICE_ROLE=worker`, `ENVIRONMENT=production`,
  `DATABASE_URL`, `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`,
  `INVESTIGATION_WORKER_URL`, `INVESTIGATION_WORKER_AUDIENCE`,
  `RECOVERY_SERVICE_ACCOUNT_EMAIL`, `RECOVERY_AUDIENCE`.
- **Action (representative):** deploy **without**
  `--allow-unauthenticated`, so Cloud Run requires authentication at the
  gateway in addition to the in-app OIDC check:

  ```bash
  gcloud run deploy <WORKER_SERVICE> \
    --image=<...>:<TAG> --region=<REGION> \
    --set-env-vars=SERVICE_ROLE=worker,ENVIRONMENT=production,... \
    --set-secrets=DATABASE_URL=...
  ```

- **Expected verification:** an unauthenticated request to
  `/api/v1/worker/investigate` is rejected at the gateway (403) or by the app
  (401/503); a request with a valid `<TASKS_SA>` OIDC token reaches the app.
- **Resource / cost / live change:** creates a Cloud Run service (compute
  cost).
- **Note:** `INVESTIGATION_WORKER_AUDIENCE` is the service **root URL**, while
  `INVESTIGATION_WORKER_URL` includes the `/api/v1/worker/investigate` path.
  Set both explicitly (see `cloud-tasks-deployment.md`).

### F. Cloud Tasks queue and service identities — CLOUD

- **Prerequisites:** project APIs `cloudtasks.googleapis.com` and
  `run.googleapis.com` enabled.
- **Identities (three, and they are distinct):**
  1. **Task creator** — the API service's Cloud Run runtime service account
     (`<API_RUNTIME_SA>`), used implicitly via Application Default
     Credentials. The application does **not** configure this identity; it is
     whatever the API service runs as. It needs `cloudtasks.tasks.create` on
     the queue (`roles/cloudtasks.enqueuer`) and
     `iam.serviceAccounts.actAs` on `<TASKS_SA>`
     (`roles/iam.serviceAccountUser`) to attach an `oidc_token`.
  2. **Token minter / accepted worker identity** — `<TASKS_SA>` =
     `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`. It is both the account minted into
     the token and the **only** account the worker accepts. It needs
     `roles/run.invoker` on `<WORKER_SERVICE>`.
  3. **Recovery caller** — `<RECOVERY_SA>` = `RECOVERY_SERVICE_ACCOUNT_EMAIL`,
     with its own `RECOVERY_AUDIENCE`. It needs `roles/run.invoker` on the
     worker/recovery service and whatever lets it mint its OIDC token.
- **Action (representative):**

  ```bash
  gcloud tasks queues create <QUEUE> --location=<REGION> --max-attempts=5 --max-retry-duration=...
  gcloud run services add-iam-policy-binding <WORKER_SERVICE> \
    --member=serviceAccount:<TASKS_SA> --role=roles/run.invoker --region=<REGION>
  gcloud tasks queues add-iam-policy-binding <QUEUE> \
    --member=serviceAccount:<API_RUNTIME_SA> --role=roles/cloudtasks.enqueuer --location=<REGION>
  gcloud iam service-accounts add-iam-policy-binding <TASKS_SA> \
    --member=serviceAccount:<API_RUNTIME_SA> --role=roles/iam.serviceAccountUser
  ```

- **Queue assumptions:** the app does **not** create the queue; it assumes an
  HTTP-target queue exists. Per-task `oidc_token` is set in code, so no
  queue-level OIDC config is needed. Set `max_attempts` small (default 100 is
  too high — see `cloud-tasks-deployment.md` for why non-claim responses do
  not consume an application attempt).
- **Retry/deadline:** HTTP tasks default to a 10-minute `dispatch_deadline`;
  the placeholder worker replies immediately, but a real investigation must
  finish below that (or the lease must be renewed).
- **Expected verification:** one real enqueue succeeds; one real delivery
  returns `200`; a duplicate delivery returns `200 already_completed`.
- **Resource / cost / live change:** creates service accounts, IAM bindings,
  and a queue. **Status: unexercised — no Cloud Tasks API call has ever been
  made against real Google Cloud.**

### G. Environment variables and Secret Manager — CLOUD

- **Secrets (Secret Manager, referenced with `--set-secrets`):**
  `DATABASE_URL`, `GRAFANA_WEBHOOK_PASSWORD`, `INCIDENT_API_PASSWORD` (and the
  two `*_USERNAME` values if treated as sensitive). Use **different** secrets
  for `GRAFANA_WEBHOOK_PASSWORD` and `INCIDENT_API_PASSWORD` — never reuse one
  as the other.
- **Non-secret env vars:** `SERVICE_ROLE`, `ENVIRONMENT`, `DATABASE_SCHEMA`
  (fixed), `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`,
  `INVESTIGATION_WORKER_URL`, `INVESTIGATION_WORKER_AUDIENCE`,
  `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`, `RECOVERY_SERVICE_ACCOUNT_EMAIL`,
  `RECOVERY_AUDIENCE`, `INVESTIGATION_JOB_LEASE_SECONDS`,
  `INCIDENT_API_USERNAME`, `GRAFANA_WEBHOOK_USERNAME`.
- **Which service:** the **API** service needs the dispatch + Grafana +
  incident-API variables; the **worker** service needs the worker/recovery +
  database variables. They can share one `DATABASE_URL` secret.
- **Expected verification:** `/health/ready` passes; logs contain no
  connection string, password, or token.
- **Resource / cost / live change:** creates secret versions (minimal cost).
- **Note:** the readiness endpoint logs only the exception *type*, and the
  auth paths log no token/traceback — verified in code and tests.

### H. Grafana webhook integration — changes a LIVE service

- **Prerequisites:** the API service URL; the webhook credentials from (G).
- **Action:** in Grafana, add a webhook contact point pointing at
  `https://<API_SERVICE URL>/api/v1/webhooks/grafana` with HTTP Basic auth set
  to `GRAFANA_WEBHOOK_USERNAME` / `GRAFANA_WEBHOOK_PASSWORD`; attach it to the
  relevant alert rule.
- **Expected verification:** one real firing alert creates an incident
  (`created=1`); a repeat is `updated=1` with no duplicate; a resolved alert
  sets the incident `completed`.
- **Resource / cost / live change:** **this modifies Micuppa's production
  Grafana alerting.** Coordinate; it is the only step that touches a live
  Micuppa service.
- **Do not** point production Grafana at a half-configured API service: the
  webhook answers `503` until credentials exist, so Grafana will report
  delivery failures rather than silently drop alerts.

### I. End-to-end smoke test and monitoring — CLOUD

- **Prerequisites:** D–H complete.
- **Action / verification:**
  1. Send **one** real alert and follow the job:
     `pending → queued → running → completed`
     (inspect `incident_agent.investigation_jobs`).
  2. Force a duplicate Cloud Tasks delivery and confirm `already_completed`.
  3. Configure a recovery caller (e.g. Cloud Scheduler) to
     `POST /api/v1/internal/recovery` with the `<RECOVERY_SA>` OIDC token;
     verify a stale lease is reclaimed exactly once.
  4. Configure a bounded queue `max_attempts` and verify a deliberately
     failing job exhausts it.
  5. Add log-based alerts on the dispatcher's non-retryable `ERROR` line and
     on `/health/ready` `503`s.
- **Expected verification:** the job reaches `completed`; the duplicate is
  acked; recovery counters are as expected; no secrets appear in logs.
- **Resource / cost / live change:** adds a scheduler and alerting policies
  (minimal cost).
- **Remember:** the worker's investigation is still
  `run_investigation_placeholder` — this path proves delivery, auth, and the
  job lifecycle, **not** real investigation output.
