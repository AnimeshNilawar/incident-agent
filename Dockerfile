# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Micuppa Incident Agent — container image.
#
# The SAME image is deployed twice: once with SERVICE_ROLE=api and once with
# SERVICE_ROLE=worker (see docs/deployment.md). Cloud Run authenticates at the
# service level, so the API and the OIDC-protected worker cannot share one
# service without exposing the worker publicly.
#
# Nothing here provisions, deploys, or contacts a cloud resource.
# ---------------------------------------------------------------------------

# 3.13 and 3.14 are both supported (see .github/workflows/ci.yml); pin the
# minor series so image rebuilds are reproducible.
FROM python:3.14-slim

# Reproducible, non-interactive, and readable logs from day one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# ca-certificates is required for TLS to the database pooler and to Google's
# APIs. No build toolchain is installed: every dependency in requirements.txt
# ships a manylinux wheel, so the image stays free of a compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install pinned dependencies first so the layer is cached across code changes.
# requirements.txt pins exact versions; .dockerignore keeps tests, docs,
# .env files, and SQLite databases out of the build context.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code only. Migrations and alembic.ini are included so a future
# migration job can reuse this image, but nothing runs them at startup.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 --user-group appuser
USER appuser

# Cloud Run supplies PORT; the default above keeps `docker run` working
# locally. The exec form makes uvicorn PID 1 so it receives SIGTERM and
# shuts down cleanly instead of being killed abruptly.
#
# `--proxy-headers --forwarded-allow-ips '*'` trusts Cloud Run's front end to
# set X-Forwarded-*. That is safe only because the container is not directly
# reachable: the services run behind Cloud Run's authenticated ingress.
EXPOSE ${PORT}

# No `alembic upgrade head` and no `create_all` for PostgreSQL here: a deployed
# environment must be migrated explicitly (see docs/database-and-migrations.md).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
