import json
import uuid

from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    BadGateway,
    DeadlineExceeded,
    GatewayTimeout,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
    TooManyRequests,
    Unknown,
)
from google.cloud import tasks_v2

from app.config import settings

# Cloud Tasks API failures that are worth retrying. Everything else
# (permission denied, not found, invalid argument, missing configuration)
# cannot succeed on a plain retry and is surfaced differently by the caller.
RETRYABLE_CLOUD_TASKS_ERRORS: tuple[type[BaseException], ...] = (
    Aborted,
    BadGateway,
    DeadlineExceeded,
    GatewayTimeout,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
    TooManyRequests,
    Unknown,
)


def is_retryable_error(exc: BaseException) -> bool:
    """Return ``True`` when a Cloud Tasks failure is likely transient.

    The classification drives how the dispatcher logs the failure. It does
    not decide recovery: a job whose dispatch failed is always left
    ``pending`` so a later sweep can retry it.
    """
    return isinstance(exc, RETRYABLE_CLOUD_TASKS_ERRORS)


def enqueue_investigation(
    job_id: uuid.UUID,
    incident_id: uuid.UUID,
    client: tasks_v2.CloudTasksClient | None = None,
) -> str:
    """Enqueue an investigation using a deterministic Cloud Tasks task ID."""
    required_settings = {
        "GCP_PROJECT_ID": settings.gcp_project_id,
        "CLOUD_TASKS_LOCATION": settings.cloud_tasks_location,
        "CLOUD_TASKS_QUEUE": settings.cloud_tasks_queue,
        "INVESTIGATION_WORKER_URL": settings.investigation_worker_url,
        "CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL": (
            settings.cloud_tasks_service_account_email
        ),
    }

    missing = [
        name for name, value in required_settings.items() if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing Cloud Tasks configuration: {', '.join(missing)}"
        )

    tasks_client = client or tasks_v2.CloudTasksClient()

    parent = tasks_client.queue_path(
        settings.gcp_project_id,
        settings.cloud_tasks_location,
        settings.cloud_tasks_queue,
    )

    task_name = tasks_client.task_path(
        settings.gcp_project_id,
        settings.cloud_tasks_location,
        settings.cloud_tasks_queue,
        f"investigation-{job_id}",
    )

    payload = {
        "job_id": str(job_id),
        "incident_id": str(incident_id),
    }

    audience = settings.effective_worker_audience

    task = {
        "name": task_name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": settings.investigation_worker_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode("utf-8"),
            "oidc_token": {
                "service_account_email": (
                    settings.cloud_tasks_service_account_email
                ),
                "audience": audience,
            },
        },
    }

    try:
        response = tasks_client.create_task(
            request={"parent": parent, "task": task}
        )
        return response.name
    except AlreadyExists:
        # A retry may attempt to enqueue a task that already exists.
        return task_name
