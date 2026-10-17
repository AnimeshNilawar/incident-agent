"""Test-wide configuration.

``app.config.Settings`` loads a ``.env`` file, and a developer's real ``.env``
usually points at a hosted database and holds live credentials. The test suite
must never read it: importing ``.env`` would both change the values the tests
assert on and, because ``app.db.session`` builds its engine and
``app.db.base`` derives ``Base.metadata.schema`` at import time, could open a
connection to a real database.

This module therefore pins every setting to its local/test default *before* any
application module is imported. Process environment variables take precedence
over ``.env``, so these values shadow the developer's file. Every value is also
the corresponding ``Settings`` default, so the suite behaves exactly as it did
without any ``.env`` present.

Tests that construct ``Settings()`` themselves — for example after clearing the
environment — are covered too: the ``.env`` file is disabled for the whole test
run below. This is test-only; the application still loads ``.env`` at runtime.
"""

from __future__ import annotations

import os

# Pin the full configuration before ``app.config`` is imported. Keep
# ``ENVIRONMENT`` as ``local`` (not ``test``) so the application lifespan keeps
# creating the SQLite tables for the local-development test path.
_TEST_ENVIRONMENT = {
    "ENVIRONMENT": "local",
    "SERVICE_ROLE": "all",
    "DATABASE_URL": "sqlite:///./incident_agent.db",
    "DATABASE_SCHEMA": "incident_agent",
    "GRAFANA_WEBHOOK_USERNAME": "",
    "GRAFANA_WEBHOOK_PASSWORD": "",
    "INCIDENT_API_USERNAME": "",
    "INCIDENT_API_PASSWORD": "",
    "GCP_PROJECT_ID": "",
    "CLOUD_TASKS_LOCATION": "asia-south1",
    "CLOUD_TASKS_QUEUE": "incident-investigations",
    "INVESTIGATION_WORKER_URL": "",
    "CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL": "",
    "INVESTIGATION_WORKER_AUDIENCE": "",
    "RECOVERY_SERVICE_ACCOUNT_EMAIL": "",
    "RECOVERY_AUDIENCE": "",
    "INVESTIGATION_JOB_LEASE_SECONDS": "900",
}

os.environ.update(_TEST_ENVIRONMENT)

# Imported after the pin above, so ``Settings`` (and the module-level
# ``settings``) is built from the test-safe values rather than ``.env``.
from app.config import Settings

# Stop later ``Settings()`` constructions (for example in tests that delete the
# ambient variables) from falling back to the developer's real ``.env``.
Settings.model_config["env_file"] = None
