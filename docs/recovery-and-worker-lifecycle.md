# Investigation Recovery and Worker Lifecycle

This document describes how investigation jobs are claimed, executed,
retried, and recovered. It covers the internal recovery endpoint, the
concurrency-safe claim/lease model, and the PostgreSQL integration suite
that verifies the concurrency guarantees.

Cloud Tasks queue assumptions, OIDC/audience configuration, IAM, and the
per-response retry semantics are documented separately in
[`cloud-tasks-deployment.md`](cloud-tasks-deployment.md). The container
image, health checks, and the API/worker Cloud Run split are in
[`deployment.md`](deployment.md).

## Job state model

`InvestigationJob.status` moves through:

```
pending -> queued -> running -> completed
                            \-> failed
```

- `pending`: created, not yet enqueued.
- `queued`: enqueued to Cloud Tasks with a deterministic task name.
- `running`: claimed by a worker for the duration of one lease.
- `completed`: terminal success.
- `failed`: terminal failure **or** a transient failure awaiting redelivery.

`failed` is claimable again so a Cloud Tasks redelivery can retry a
transient failure. `max_attempts` (default 3) bounds how often.

The attempt counter `attempts` increments exactly once per successful
claim and is never reset by recovery, so retry limits cannot be bypassed.

## Worker claim, lease, and fencing

The worker endpoint is `POST /api/v1/worker/investigate`.

Claiming is a single atomic conditional `UPDATE` (`app/services/job_lifecycle.py`):

```sql
UPDATE investigation_jobs
SET status = 'running',
    attempts = attempts + 1,
    started_at = :now,
    updated_at = :now,
    last_error = NULL
WHERE id = :job_id
  AND incident_id = :incident_id
  AND status IN ('pending', 'queued', 'failed')
  AND attempts < max_attempts;
```

If the statement affects one row, this worker owns the job. If it affects
zero rows, the worker diagnoses why (not found, already completed, already
running, attempt limit reached, or not claimable) and responds accordingly.

Design properties:

- **Atomicity.** The `UPDATE` is the claim. It does not use SQLite's
  no-op `with_for_update()`. On PostgreSQL the `UPDATE` takes a row lock,
  so two concurrent deliveries cannot both claim the same row under
  `READ COMMITTED`.
- **Fencing token.** The post-claim `attempts` value is the claim token.
  Completing or failing a job is another conditional `UPDATE` gated on
  `status = 'running' AND attempts = :claim_token`. A stale worker whose
  job was reclaimed by someone else can never overwrite newer state.
- **Lease.** `started_at` records when the current claim began. A running
  job is considered abandoned only when `started_at` is older than the
  configured lease window (`INVESTIGATION_JOB_LEASE_SECONDS`, default
  900s). Recovery never reclaims a job just because `updated_at` is old.
- **Release.** `complete_job` / `fail_job` clear the running state inside
  the same fenced `UPDATE`.

The current worker is a placeholder (`run_investigation_placeholder`) that
completes within a single request, so full lease **renewal** is not yet
implemented. See "Known limitations" below.

## Duplicate delivery

- An already `completed` job returns `200 {"status": "already_completed"}`
  without launching a second investigation.
- A delivery that arrives while the job is `running` returns `409` so
  Cloud Tasks retries later; it does not start a second investigation.
- If a worker finishes after its lease expired and another worker has
  already reclaimed and advanced the job, the stale worker's fenced
  completion affects zero rows and the endpoint returns
  `200 {"status": "superseded"}` so Cloud Tasks stops redelivering.

## Recovery

`recover_investigation_jobs()` (`app/services/investigation_recovery.py`)
performs a bounded, idempotent sweep:

1. **Old pending jobs** (`status = 'pending'` and `updated_at` older than
   `pending_after_seconds`) are re-dispatched. This covers jobs that were
   never successfully enqueued.
2. **Abandoned running jobs** (`status = 'running'` and `started_at` older
   than `stale_running_after_seconds`) are reclaimed back to `pending` with
   a conditional `UPDATE` fenced on `status`, `attempts`, and
   `started_at <= lease_cutoff`. A job with no `started_at` is treated as
   not provably abandoned and left untouched.
3. **Exhausted jobs** (`attempts >= max_attempts` in a non-terminal state)
   are terminalized to `failed` instead of being requeued, in both the
   pending and running paths.

Counters returned: `pending_found`, `stale_running_recovered`,
`dispatched`, `enqueue_failed`, `attempts_exhausted`.

Safety and load properties:

- **Never resets attempts**, so recovery cannot bypass `max_attempts`.
- **Bounded**: each query is capped by `batch_size`.
- **Idempotent enqueue**: Cloud Tasks task names are deterministic
  (`investigation-<job_id>`) and a duplicate enqueue is treated as success
  (`AlreadyExists`), so concurrent recovery runs or a Cloud Tasks
  redelivery cannot create duplicate tasks.
- **Queue failures are recoverable**: a failed enqueue leaves the job
  `pending` with `last_error` set, so a later sweep retries it.
- **Duplicate recovery requests** do not create duplicate active
  investigations: the atomic per-job claim in the worker is the final
  guard, and reclaimed jobs are re-checked (`status == 'pending'`) before
  dispatch.

## Internal recovery endpoint

`POST /api/v1/internal/recovery` (`app/api/internal.py`).

### Authentication

The endpoint verifies a Google OIDC ID token sent as
`Authorization: Bearer <token>`:

- The token audience must equal `RECOVERY_AUDIENCE`.
- The token `email` must equal `RECOVERY_SERVICE_ACCOUNT_EMAIL` and the
  token must assert `email_verified`.

This is intentionally separate from the Cloud Tasks worker audience and
service account, so a task-delivery token cannot trigger recovery.

The endpoint fails closed:

| Condition                                | Status |
|------------------------------------------|--------|
| Missing `Authorization: Bearer` header   | 401    |
| Invalid token                            | 401    |
| Token from a different service account   | 403    |
| Recovery auth configuration missing      | 503    |
| Invalid query parameters                 | 422    |
| Recovery argument rejected by the service| 400    |
| Success                                  | 200    |

The dependency `verify_recovery_request` is overridable in tests
(`app.dependency_overrides`), which keeps verification separate from
endpoint behavior and avoids any need for real cloud credentials.

Responses contain only counters — never tokens, stack traces, database
URLs, or other secrets.

### Query parameters

| Parameter                     | Default | Bounds         |
|-------------------------------|---------|----------------|
| `pending_after_seconds`       | 30      | 0–86400        |
| `stale_running_after_seconds` | lease seconds | 1–604800 |
| `batch_size`                  | 100     | 1–500          |

`stale_running_after_seconds` is resolved from the **current** settings
(`INVESTIGATION_JOB_LEASE_SECONDS`) inside the request handler when the
caller omits it, so a runtime settings change takes effect immediately. It
is not frozen from the settings value at import time.

### Response body

```json
{
  "status": "ok",
  "pending_found": 0,
  "stale_running_recovered": 0,
  "dispatched": 0,
  "enqueue_failed": 0,
  "attempts_exhausted": 0
}
```

### Configuration

```
RECOVERY_SERVICE_ACCOUNT_EMAIL=
RECOVERY_AUDIENCE=
INVESTIGATION_JOB_LEASE_SECONDS=900
```

At runtime the recovery endpoint assumes these are bound to values for a
Cloud Scheduler job (or equivalent authorized caller) that invokes the
endpoint on a fixed schedule.

## Retry-limit alignment

- Application-level: `attempts >= max_attempts` terminalizes the job
  (`attempt_limit_reached` / `attempts_exhausted`).
- Queue-level: Cloud Tasks should be configured to retry transient `5xx`
  responses but not to outlive the application limit. The application
  limit is authoritative; when Cloud Tasks redelivers past it, the worker
  returns `attempt_limit_reached` without running work.

Cloud Tasks HTTP delivery is at-least-once and treats **only a 2xx response
as success**, so every terminal or idempotent outcome returns `2xx` and every
"retry later" outcome returns a non-2xx. Because the application only
increments `attempts` when a claim succeeds, non-claim rejections (409/404/
401/403/503) are bounded only by the queue's retry config — which is why a
small queue `max_attempts` matters. The full response-by-response mapping,
queue configuration assumptions, and the required IAM bindings are in
[`cloud-tasks-deployment.md`](cloud-tasks-deployment.md).

## Known limitations and PostgreSQL integration tests

The concurrency logic uses portable atomic conditional `UPDATE`
statements, so it behaves correctly on both SQLite and PostgreSQL for
single-row fencing. SQLite serializes writers, however, so it cannot prove
the PostgreSQL `READ COMMITTED` behavior of two truly concurrent claims.
That behavior is now covered by a dedicated, opt-in PostgreSQL integration
suite: `tests/integration/test_postgres_concurrency.py`.

The suite verifies, against real PostgreSQL transactions:

1. Two concurrent `claim_job` calls on the same row — exactly one succeeds,
   the other reports the job is already running, and `attempts` increments
   exactly once.
2. A stale worker whose lease was reclaimed cannot overwrite the newer
   claim: its `complete_job`/`fail_job` affect zero rows while the newer
   fencing token still finalizes, and `attempts` is never reset.
3. Two concurrent `recover_investigation_jobs` runs reclaim an abandoned
   running job at most once and never leave a job in duplicate active
   state.
4. A failed enqueue leaves jobs `pending` (recoverable) with `last_error`
   set and does not reset `attempts`.
5. Attempt-limit races: the counter never exceeds `max_attempts`,
   exhausted jobs are terminalized to `failed` (not requeued), and a stale
   worker cannot finalize an exhausted or superseded claim.

### Test database isolation

- The suite runs **only** when `POSTGRES_TEST_DATABASE_URL` is explicitly
  set; otherwise every integration test is skipped and the ordinary SQLite
  suite runs on its own.
- `POSTGRES_TEST_DATABASE_URL` must point at a dedicated PostgreSQL test
  server. The URL is used only to `CREATE DATABASE` / `DROP DATABASE`; it
  is never the production Supabase connection and has no default.
- Each test creates a **uniquely named throwaway database**, applies the real
  Alembic migrations inside it, and drops it afterwards. No existing/shared
  database is ever truncated or modified, and tests are fully isolated from
  each other.
- If the configured URL appears to target a hosted/production host
  (`supabase`, `amazonaws`, `rds`, `neon.tech`, `azure`, `cloudsql`,
  `googleusercontent`, `heroku`), the fixture fails loudly **before**
  opening any connection.

### Running the tests

Ordinary suite (SQLite, no PostgreSQL required). `pytest.ini` sets
`pythonpath = .`, so no `PYTHONPATH` is needed:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

PostgreSQL integration suite (requires a local, isolated PostgreSQL):

```powershell
# Example: an ephemeral container. Any reachable test-only server works.
docker run -d --name pg-integration-test `
  -e POSTGRES_USER=inctest `
  -e POSTGRES_PASSWORD=inctest-local-only `
  -e POSTGRES_DB=incident_agent_maintenance `
  -p 127.0.0.1:55432:5432 postgres:16

$env:POSTGRES_TEST_DATABASE_URL = "postgresql+psycopg://inctest:inctest-local-only@127.0.0.1:55432/incident_agent_maintenance"
.\.venv\Scripts\python.exe -m pytest -m postgres_integration -v
```

Integration tests are marked `postgres_integration` (registered in
`pytest.ini`). They are never run against a real, shared, or production
database. Both suites also run automatically in CI
(`.github/workflows/ci.yml`) against an ephemeral `postgres:16` service
container.

Lease renewal is not implemented because the current investigation body is
a placeholder that completes within one request. A future real executor
should renew `started_at` (or add an explicit `lease_expires_at` column)
while investigating, and recovery should honor that column.
