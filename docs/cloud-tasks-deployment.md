# Cloud Tasks Dispatch and Worker Authentication

This document covers the production readiness of the Cloud Tasks integration:
required environment variables, queue and worker URL assumptions, OIDC
audience and service-account requirements, least-privilege IAM, retry and
duplicate-delivery semantics, and a safe future deployment checklist.

It describes what the **application** does. No Google Cloud resource, IAM
binding, queue, service, or deployment has been created or modified — see
"Safe future deployment checklist" below.

The container image that would run these services, its health checks, and the
recommended API/worker Cloud Run split are described in
[`deployment.md`](deployment.md).

## Required environment variables

All values are supplied by the environment. None are hard-coded, none are
logged, and none are committed (`.env` is git-ignored).

| Variable | Required for dispatch | Required for worker | Notes |
|---|---|---|---|
| `GCP_PROJECT_ID` | yes | no | Project that owns the queue. |
| `CLOUD_TASKS_LOCATION` | yes | no | Queue location, e.g. `asia-south1`. |
| `CLOUD_TASKS_QUEUE` | yes | no | Queue name, e.g. `incident-investigations`. |
| `INVESTIGATION_WORKER_URL` | yes | yes | Full HTTPS URL of `POST /api/v1/worker/investigate`. |
| `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL` | yes | yes | Service account minted as the OIDC caller, and the only accepted identity on the worker. |
| `INVESTIGATION_WORKER_AUDIENCE` | recommended | recommended | OIDC audience. Falls back to `INVESTIGATION_WORKER_URL` when empty. |
| `RECOVERY_SERVICE_ACCOUNT_EMAIL` | no | no | Separate identity for `POST /api/v1/internal/recovery`. |
| `RECOVERY_AUDIENCE` | no | no | Separate audience for recovery. |
| `INVESTIGATION_JOB_LEASE_SECONDS` | no | no | Default 900. Claim lease used by recovery. |

Dispatch fails fast: `enqueue_investigation()` refuses to build a task when
`GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`,
`INVESTIGATION_WORKER_URL`, or `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL` is empty,
raising a `RuntimeError` that names the missing variables. Nothing is marked
queued, so the job stays `pending` and remains visible in `last_error`.

## Audience consistency

The dispatcher mints the token and the worker verifies it, so both sides must
resolve the same audience. Both call `settings.effective_worker_audience`,
which returns `INVESTIGATION_WORKER_AUDIENCE` if set and otherwise
`INVESTIGATION_WORKER_URL`. There is a single code path for this resolution
precisely so the two sides cannot drift apart.

> **Set `INVESTIGATION_WORKER_AUDIENCE` explicitly on Cloud Run.** The
> fallback value is the *full request URL including the endpoint path*
> (`https://SERVICE.run.app/api/v1/worker/investigate`), while Cloud Run's
> gateway validates the token audience against the *service* URL. Prefer the
> service root URL (`https://SERVICE.run.app`) or a dedicated custom audience,
> and configure the same value in this variable (the same string is used by
> both the minter and the verifier). Confirm the exact match with one real
> delivery before relying on it.

## Worker authentication

`POST /api/v1/worker/investigate` verifies a Google OIDC ID token sent as
`Authorization: Bearer <token>` using `google.oauth2.id_token.verify_oauth2_token`,
which checks the signature, issuer, audience, and expiry in one call. Only
after that succeeds are the claims read:

- `email` must equal `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`.
- `email_verified` must be literally `True`.

Fail-closed behavior:

| Condition | Status |
|---|---|
| Missing `Authorization` header / non-Bearer scheme | 401 |
| Invalid, malformed, or expired token | 401 |
| Token cannot be verified (transport/verification failure) | 503 |
| `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL` or effective audience not configured | 503 |
| Verified token from a different service account | 403 |
| `email_verified` not `True` / no `email` claim | 403 |
| Verification succeeded and identity matched | request proceeds |

A 503 (configuration/verification) still counts as a non-2xx response, so
Cloud Tasks will retry it — the service does not acknowledge an unverified
request with a 2xx.

### What cannot be verified without a real Google token

`tests/test_worker_auth.py` patches `verify_oauth2_token`, so the following
are **not** proven locally and require one real Cloud Tasks delivery:

1. Signature validation against Google's public keys (and token issuer/
   certificate fetching), because forging a Google-signed token needs a real
   Google service account.
2. That Cloud Run's gateway accepts the audience we send — this depends on
   the exact audience string described above.
3. That Cloud Tasks actually attaches the `oidc_token` with the configured
   service account (requires `iam.serviceAccounts.actAs`).

## Task payload

The task body contains exactly two fields and nothing else:

```json
{"job_id": "<uuid>", "incident_id": "<uuid>"}
```

No tokens, no credentials, no database URLs, no incident content. Every other
value the worker needs (lease, audience, service account, database) comes from
its own environment, not from the message.

## Deterministic naming and idempotency

- Task name: `investigation-<job_id>` via `task_path(...)`. The format
  (`hex + hyphens`) satisfies Cloud Tasks' task-ID character rules, and the
  name encodes the queue so it is unique per job.
- A duplicate enqueue (recovery re-sweep, concurrent recovery runs, or a
  retry after a lost response) raises `AlreadyExists`, which is caught and
  returned as success — so retries cannot create duplicate tasks.
- `enqueue_investigation()` returns the deterministic task name on both
  paths, so `InvestigationJob.cloud_task_name` is stable.

## Queue configuration assumptions

Nothing creates the queue. The application assumes a queue already exists at
`GCP_PROJECT_ID` / `CLOUD_TASKS_LOCATION` / `CLOUD_TASKS_QUEUE` with an HTTP
target; the per-task `oidc_token` is set in code, so no queue-level OIDC
configuration is required.

Recommended queue settings (verify against your own service-level targets):

- **Retry `max_attempts`.** The Cloud Tasks default is `100`. Because the
  application only increments `attempts` when a claim *succeeds*, responses
  that are not claims (409 while running, 404 mismatch, 401/403 auth failure,
  503 not-configured) do not consume an application attempt. A queue default
  of 100 therefore means a permanently broken condition can be redelivered up
  to 100 times. Set `max_attempts` to a small value (e.g. 5) and a bounded
  `max_retry_duration` so permanent failures are exhausted quickly.
- **`min_backoff` / `max_backoff` / `max_doublings`.** Keep sensible backoff;
  Cloud Tasks already increases backoff on `429` and `503` and honors
  `Retry-After`.
- **`dispatch_deadline`.** HTTP tasks default to 10 minutes. The placeholder
  worker replies within one request, but a future investigation must be
  bounded below this deadline (or renew its lease — see "Known limitations"
  in `recovery-and-worker-lifecycle.md`).
- **Rate limits** (`max_dispatches_per_second`, `max_burst`) should start at
  conservative values.

Retry configuration is queue-level, not set per task; keeping it in one
place makes the queue the single operational control.

## Retry and duplicate-delivery semantics

Cloud Tasks HTTP delivery is **at-least-once**. For HTTP targets, a
**2xx response is the only success signal**; any other status code (or no
response) is retried according to the queue's retry configuration.

Worker responses are chosen deliberately:

| Worker response | Cloud Tasks result | Application effect |
|---|---|---|
| `200 completed` | acked | terminal success |
| `200 already_completed` | acked | idempotent redelivery of a finished job |
| `200 superseded` | acked | stale worker fenced out; newer attempt owns the job |
| `200 attempt_limit_reached` | acked | stops retrying once `attempts >= max_attempts` |
| `409` already running / not claimable | retried | no state change, no attempt consumed |
| `404` job not found / incident mismatch | retried | no state change |
| `500` investigation raised | retried | job `failed`, one attempt recorded |
| `401` / `403` authentication rejected | retried | no state change |
| `503` auth not configured / verification failure | retried | no state change |

Because a **2xx acks the task**, every terminal or idempotent outcome returns
200, while every "please try again later" outcome returns a non-2xx. This is
the only lever Cloud Tasks exposes for stopping retries.

The application's `max_attempts` remains authoritative: after the third claim
the worker returns `200 attempt_limit_reached` and stops executing work, so a
queue configured above `max_attempts` wastes deliveries but cannot exceed the
application limit.

### Dispatch failures

A Cloud Tasks API failure during enqueue never marks the job queued. The job
is left `pending` with `last_error` set, so a later recovery sweep retries it.
Failures are classified:

- **Retryable** (`google.api_core.exceptions`: `ServiceUnavailable`,
  `DeadlineExceeded`, `TooManyRequests`, `InternalServerError`, `Aborted`,
  `GatewayTimeout`, `BadGateway`, `ResourceExhausted`, `Unknown`) — logged at
  `WARNING`; expected to clear on a later sweep.
- **Everything else** (`PermissionDenied`, `Unauthenticated`, `NotFound`,
  `InvalidArgument`, and configuration `RuntimeError`) — logged at `ERROR` so
  it is alertable, because no amount of retrying will fix it.

Dispatch failures do **not** increment `attempts` and are deliberately *not*
terminalized, so fixing configuration lets the job recover without operator
surgery. The corollary: a permanently broken configuration will retry once per
recovery sweep indefinitely — alert on the `ERROR` log line rather than
expecting the job to fail on its own.

## Least-privilege IAM

No IAM policy has been changed. A future deployment needs exactly these
bindings (use the current GCP documentation as the authority for role names):

1. **Dispatcher identity** (the service running this application):
   - `cloudtasks.tasks.create` on the queue — normally via `roles/cloudtasks.enqueuer`.
   - `iam.serviceAccounts.actAs` on the `CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`
     account — normally via `roles/iam.serviceAccountUser` on that account.
     Without this, `create_task` with an `oidc_token` is rejected.
2. **`CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL`** (the identity inside the token):
   - `run.routes.invoke` on the worker service — normally via `roles/run.invoker`.
     This is what makes Cloud Run accept the delivery at all.
3. **Worker Cloud Run service**: authenticated-only traffic (reject
   unauthenticated requests at the gateway) plus the application-level OIDC
   check above. The runtime service account needs only its database privileges
   (see `database-and-migrations.md`) — it needs no Cloud Tasks permission.
4. **Recovery caller** (e.g. Cloud Scheduler): `iam.serviceAccounts.actAs` on
   the SA whose OIDC token it mints, plus `run.routes.invoke` on the recovery
   endpoint. Recovery uses `RECOVERY_SERVICE_ACCOUNT_EMAIL` /
   `RECOVERY_AUDIENCE`, which are intentionally distinct from the worker's, so
   a task-delivery token cannot trigger recovery.
5. **APIs**: `cloudtasks.googleapis.com` and `run.googleapis.com` enabled in
   the project.

Nothing here grants broad owner/editor access; each service account receives
only the one permission it acts on.

## Safe future deployment checklist

Nothing below has been performed. Each step is a deliberate, reviewable
action to be taken later:

1. Create the queue in the chosen location with HTTP target dispatch mode and
   the retry settings above.
2. Create (or select) the worker invoker service account; grant it no roles
   beyond `run.routes.invoke` on the worker service.
3. Grant the dispatcher `cloudtasks.enqueuer` on that queue and
   `iam.serviceAccountUser` on the invoker account.
4. Enable `cloudtasks.googleapis.com` and `run.googleapis.com`.
5. Deploy the application with the required variables set, and set
   `INVESTIGATION_WORKER_AUDIENCE` explicitly to the service's audience.
6. Confirm the worker service rejects unauthenticated requests at the gateway
   as well as in the application.
7. Send **one** real task and verify end to end: task created, OIDC token
   accepted, job reaches `completed`, and a duplicate delivery is acked as
   `already_completed`.
8. Configure a bounded queue `max_attempts` and verify a deliberately failing
   job exhausts it.
9. Configure the recovery scheduler and verify a pending job with an expired
   lease is reclaimed exactly once.
10. Add a log-based alert on the dispatcher's non-retryable `ERROR` line.

## Tests

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
```

The Cloud Tasks, dispatcher, and worker-auth tests use mocked clients only —
they never contact the real Cloud Tasks API. The PostgreSQL concurrency suite
still runs only against an explicitly configured disposable test database
(see `recovery-and-worker-lifecycle.md`).
