# Database Configuration, Schema Isolation, and Migrations

This document describes how the Micuppa Incident Agent selects its database,
keeps its tables out of the host application's schema, and evolves its schema
with Alembic.

## Configuration

| Setting           | Default                            | Notes |
|-------------------|------------------------------------|-------|
| `ENVIRONMENT`     | `local`                            | `local`/`test` may use SQLite. Anything else is treated as deployed. |
| `DATABASE_URL`    | `sqlite:///./incident_agent.db`    | Deployed environments must set a PostgreSQL URL. |
| `DATABASE_SCHEMA` | `incident_agent`                   | PostgreSQL only and **fixed** (see below). Ignored for SQLite. |

Rules enforced in `app/config.py`:

- In a **deployed** environment (`ENVIRONMENT` not in `{local, test}`) an
  empty `DATABASE_URL` or a non-PostgreSQL URL is rejected at startup. The
  application never silently falls back to SQLite in production.
- `DATABASE_SCHEMA` is validated as a plain SQL identifier **and** must equal
  the fixed `incident_agent`. Any other value is rejected at startup rather
  than allowed to silently diverge from the hard-coded migrations (see
  _Why the schema is fixed_).
- A configuration failure never echoes a value. Pydantic's stock
  `ValidationError` string embeds the raw input mapping — which includes
  `DATABASE_URL` and therefore its password — so the module-level
  `Settings()` construction re-raises a **sanitized** summary
  (`app.config.format_settings_error`, built from `errors(include_input=False)`)
  that contains only each failing field's location and message. A startup
  traceback captured by the platform log therefore never carries a credential.

## Schema isolation

PostgreSQL is reached through the dedicated `incident_agent` schema so the
agent's tables, indexes, foreign keys, and unique constraints never land in
the host application's `public` schema.

- `app/db/base.py` sets the shared `MetaData` schema to `DATABASE_SCHEMA`
  **only when the URL targets PostgreSQL**. For SQLite the schema stays unset
  and all tables are unqualified, preserving the existing test setup.
- Foreign keys are declared unqualified (`ForeignKey("incidents.id")`) and
  SQLAlchemy resolves and renders them against the metadata schema, producing
  `REFERENCES incident_agent.incidents (id)`.
- `schema_translate_map` is the runtime escape hatch. The PostgreSQL
  integration tests use `schema_translate_map={None: "incident_agent"}` to
  run the same (SQLite-default) models against a migrated test database.
- The Alembic environment (`migrations/env.py`) creates the schema with
  `CREATE SCHEMA IF NOT EXISTS` and keeps Alembic's own `alembic_version`
  table inside `incident_agent`, so nothing is written to `public`.

## Connection strategy for Supabase

Supabase is typically reached through its connection pooler
(Supavisor/pgbouncer, transaction mode). In that mode server-side prepared
statements cannot be routed safely across transactions, so
`app/db/session.py`:

- uses the `psycopg` (v3) driver (`postgresql+psycopg://`) and passes
  `prepare_threshold=None` to disable automatic statement preparation;
- sets `pool_pre_ping=True` and `pool_recycle=1800` so connections the pooler
  has closed are detected and recycled;
- carries `sslmode=require` in the URL (Supabase requirement).

The `prepare_threshold` argument is only added for the `psycopg` driver;
pgbouncer/psycopg2 would reject it.

## Before connecting to Supabase (operator checklist)

Nothing in this repository connects to Supabase on its own, and nothing
migrates a hosted database automatically. Before the first connection the
operator confirms each item below — using placeholders, **never** the real
password:

| Item | Expected | Notes |
|---|---|---|
| Project / database identity | the Supabase project dedicated to the agent | confirm it in the Supabase dashboard; do not reuse the Micuppa application's schema |
| Hostname | `<SUPABASE_HOST>` | the connection-pooler host for that project |
| Port | `6543` | transaction-mode pooler (Supavisor/pgbouncer) |
| Connection mode | transaction pooler | a direct `5432` connection also works, but the pooler is the documented target (`prepare_threshold=None`) |
| Database name | `postgres` (Supabase default) or the project database | must be the intended database |
| Username | pooler form `postgres.<project-ref>`, or the dedicated roles below | see _Least-privilege access_ |
| TLS | `sslmode=require` in the URL | Supabase requires TLS |
| Schema | `incident_agent` | fixed; `DATABASE_SCHEMA` rejects any other value |
| Migration role | `USAGE` + `CREATE` on `incident_agent` | used only to run `alembic upgrade` |
| Runtime role | `USAGE` on `incident_agent` + `SELECT, INSERT, UPDATE, DELETE` on the agent tables | no DDL rights; no access to the host application's tables |

### Verify the target locally without exposing the connection string

The inspector reads `DATABASE_URL` from the same configuration the application
uses (`app.config.Settings`), so a value in `.env` is honoured and a value set
in the shell wins over it — prefer the shell so the password is not written to
disk. Let the application describe the target. It parses the URL and prints only
the non-secret parts — host, port, database, username, driver, TLS mode, and
*whether* a password is present. It opens **no** network connection, so it is
safe to run before the database is reachable:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://<USER>:<PASSWORD>@<SUPABASE_HOST>:6543/postgres?sslmode=require"
.\.venv\Scripts\python.exe -m app.db.target
```

Expected output (the password is reported only as a boolean, never printed):

```
dialect=postgresql
driver=psycopg
host=<SUPABASE_HOST>
port=6543
database=postgres
username=<USER>
password_set=True
sslmode=require
```

Confirm `host`, `port`, `sslmode`, and `username` before any migration; if
`host`/`port` are not what you expect, stop. This command is also used in
`docs/deployment.md` checklist step B.

### Confirm real connectivity and authentication (read-only)

Inspecting the target does not prove the database is reachable or that the
credentials work. Before the first hosted migration, run the read-only
connectivity check. It opens one connection and executes `SELECT 1` through the
application's own engine configuration — the same `Settings`, `build_engine`
and pooler-safe `prepare_threshold=None` path the app will use:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://<USER>:<PASSWORD>@<SUPABASE_HOST>:6543/postgres?sslmode=require"
.\.venv\Scripts\python.exe -m app.db.check_connection
```

Expected output (the same non-secret target fields, plus the result):

```
dialect=postgresql
driver=psycopg
host=<SUPABASE_HOST>
port=6543
database=postgres
username=<USER>
password_set=True
sslmode=require
connection=ok
```

- It **never** creates tables, runs migrations, or writes data — `SELECT 1` is
  the only statement executed — so it is safe to run repeatedly, including
  before the schema exists.
- On failure it prints `connection=failed` and exits `1`, deliberately omitting
  `DATABASE_URL`, the password, and the driver's error message (which can quote
  the connection string). Check the printed target's `host`, `port`, `username`
  and `sslmode` instead.
- Exit code `2` means no target is configured at all.

## Migrations

Alembic is configured in `alembic.ini` and `migrations/`. The initial
migration creates the `incident_agent` schema and both agent tables.

```powershell
# Point DATABASE_URL at the target PostgreSQL database first.
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check           # no pending model drift
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```

Key properties:

- The database URL is **never** stored in `alembic.ini`. It is resolved at
  runtime, in order, from `-x db_url=...`, a `db_url` attribute on the Alembic
  `Config` (used by the integration tests), the `sqlalchemy.url` main option,
  then `DATABASE_URL`.
- Migrations target the dedicated schema and create it explicitly, so the
  agent's tables are never created in `public`.
- `alembic check` after `upgrade head` reports **no new upgrade operations**,
  i.e. the migration accurately represents the models.
- **Migrations are never run automatically at application startup.** Local
  SQLite development still uses `Base.metadata.create_all()` for convenience;
  every PostgreSQL target (including local) must be migrated explicitly.
- `downgrade` drops the two agent tables but deliberately leaves the
  `incident_agent` schema (and `alembic_version`) in place, so Alembic can
  finish cleanly.

### Initial migration (revision `794b420b8bfc`)

The repository currently contains a **single** migration,
`794b420b8bfc_initial_incident_agent_schema`. It is the base revision
(`down_revision = None`), so `upgrade head` is expected to leave the database
at `794b420b8bfc`.

It creates — and only creates:

- the `incident_agent` schema (`CREATE SCHEMA IF NOT EXISTS`);
- `incident_agent.incidents`;
- `incident_agent.investigation_jobs` (with its indexes and the foreign key to
  `incidents`);
- `incident_agent.alembic_version` (Alembic writes its own version table into
  the same schema).

Nothing is created in `public`.

### Running it against Supabase (only when explicitly approved)

```powershell
# 1. Confirm the target first (see "Before connecting to Supabase"). No network.
$env:DATABASE_URL = "postgresql+psycopg://<MIGRATION_USER>:<PASSWORD>@<SUPABASE_HOST>:6543/postgres?sslmode=require"
.\.venv\Scripts\python.exe -m app.db.target

# 2. Apply the migrations. This connects and performs DDL.
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Post-migration checks:

```powershell
.\.venv\Scripts\python.exe -m alembic current   # -> 794b420b8bfc (head)
.\.venv\Scripts\python.exe -m alembic check     # -> no new upgrade operations
```

- `alembic current` prints `794b420b8bfc (head)`.
- `alembic check` reports **no new upgrade operations** (models match the
  database).
- Inspecting the schema shows `incidents`, `investigation_jobs`, and
  `alembic_version` inside `incident_agent`, and **nothing** added to `public`.
- The runtime role then needs only the runtime grants (below); migrations are
  not re-run by the application.

**Why this is never automatic:** `app/main.py` calls `create_all()` only for
local SQLite and only when `ENVIRONMENT=local`. PostgreSQL is migrated as an
explicit, reviewed step, so a stray `DATABASE_URL` cannot silently create or
alter schema on a hosted database at startup.

## Least-privilege access

Recommended roles (see `.env.example` for copy-paste SQL):

- **Migration role** (owner/admin): `USAGE` and `CREATE` on
  `incident_agent`; used only to run `alembic upgrade`.
- **Runtime role**: `USAGE` on `incident_agent` plus `SELECT, INSERT, UPDATE,
  DELETE` on the agent tables (and default privileges for future tables). It
  needs no `CREATE`/DDL rights and no access to the host application's
  tables.

## SQLite vs PostgreSQL behaviour

| Concern                   | SQLite (local/test)                | PostgreSQL (deployed) |
|---------------------------|------------------------------------|-----------------------|
| Schema                    | none (unqualified tables)          | `DATABASE_SCHEMA` (`incident_agent`) |
| Schema creation           | n/a                                | Alembic only (never at startup) |
| Table creation at startup | `create_all` when `environment==local` | never |
| Prepared statements       | n/a                                | disabled for psycopg pooler |

## Testing

`pytest.ini` sets `pythonpath = .`, so `import app` resolves from the
repository root and no `PYTHONPATH` needs to be set by hand.

Ordinary suite (SQLite, no PostgreSQL server required):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m "not postgres_integration"
```

PostgreSQL integration suite (requires an explicitly configured, isolated
test server):

```powershell
docker run -d --name pg-integration-test `
  -e POSTGRES_USER=inctest `
  -e POSTGRES_PASSWORD=inctest-local-only `
  -e POSTGRES_DB=incident_agent_maintenance `
  -p 127.0.0.1:55432:5432 postgres:16

$env:POSTGRES_TEST_DATABASE_URL = "postgresql+psycopg://inctest:inctest-local-only@127.0.0.1:55432/incident_agent_maintenance"
.\.venv\Scripts\python.exe -m pytest -m postgres_integration -v
```

`tests/integration/` creates a uniquely named throwaway database per test,
applies the real migrations to it, and drops it afterwards. No shared or
hosted database is ever modified, and the suite refuses to run when the URL
looks like production. If `POSTGRES_TEST_DATABASE_URL` is unset the tests are
skipped and reported as not executed.

## Why the schema is fixed (not configurable)

`DATABASE_SCHEMA` looks like a free setting but is deliberately locked to
`incident_agent`:

- The initial migration hard-codes `incident_agent`, and
  `alembic revision --autogenerate` always renders the schema as a literal, so
  every future migration would also hard-code whatever schema happened to be
  configured when it was generated. Runtime configurability would therefore be
  an illusion that silently broke as soon as the setting changed.
- Keeping migrations independent of runtime settings keeps them reproducible:
  replaying the same revision history always produces the same SQL.
- The dedicated `incident_agent` schema is specifically required (it keeps the
  agent's tables out of the host application's `public` schema), so there is no
  reason to parameterise it.

Consequently `Settings` rejects any `DATABASE_SCHEMA` other than
`incident_agent` with an explicit error. To genuinely target a different
schema you must regenerate the migrations with that schema, i.e. a code change
reviewed like any other migration.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request against a
throwaway `postgres:16` service container. It needs no Supabase, GCP, Grafana,
or other external cloud resources and no production secrets:

1. `ruff check app tests`
2. `pytest -m "not postgres_integration"` (SQLite/unit suite)
3. `alembic upgrade head` then `alembic check` (model/migration drift, with
   `DATABASE_URL` pointed at the service container)
4. `pytest -m postgres_integration` against the service container

The workflow runs on Python 3.13 and 3.14 (the project is developed on 3.14).
