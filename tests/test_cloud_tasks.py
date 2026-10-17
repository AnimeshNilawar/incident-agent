
import json
import uuid
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    DeadlineExceeded,
    InternalServerError,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    ServiceUnavailable,
    TooManyRequests,
    Unauthenticated,
)
from google.cloud import tasks_v2

from app.config import settings
from app.services.cloud_tasks import (
    enqueue_investigation,
    is_retryable_error,
)


@pytest.fixture
def configured_tasks(monkeypatch):
    monkeypatch.setattr(settings, "gcp_project_id", "test-project")
    monkeypatch.setattr(settings, "cloud_tasks_location", "asia-south1")
    monkeypatch.setattr(
        settings, "cloud_tasks_queue", "incident-investigations"
    )
    monkeypatch.setattr(
        settings,
        "investigation_worker_url",
        "https://worker.example.test/investigate",
    )
    monkeypatch.setattr(
        settings,
        "cloud_tasks_service_account_email",
        "tasks-invoker@test-project.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        settings, "investigation_worker_audience", ""
    )


def test_enqueue_creates_authenticated_http_task(configured_tasks):
    client = Mock(spec=tasks_v2.CloudTasksClient)
    client.queue_path.return_value = "queue-path"
    client.task_path.return_value = "task-path"
    client.create_task.return_value.name = "task-path"

    job_id = uuid.uuid4()
    incident_id = uuid.uuid4()

    result = enqueue_investigation(job_id, incident_id, client=client)

    assert result == "task-path"

    request = client.create_task.call_args.kwargs["request"]
    task = request["task"]

    assert request["parent"] == "queue-path"
    assert task["name"] == "task-path"
    assert task["http_request"]["http_method"] == tasks_v2.HttpMethod.POST
    assert (
        task["http_request"]["url"]
        == "https://worker.example.test/investigate"
    )

    oidc = task["http_request"]["oidc_token"]
    assert (
        oidc["service_account_email"]
        == "tasks-invoker@test-project.iam.gserviceaccount.com"
    )
    assert oidc["audience"] == "https://worker.example.test/investigate"

    payload = json.loads(task["http_request"]["body"])
    assert payload == {
        "job_id": str(job_id),
        "incident_id": str(incident_id),
    }


def test_enqueue_uses_deterministic_task_name(configured_tasks):
    client = Mock(spec=tasks_v2.CloudTasksClient)
    client.queue_path.return_value = "queue-path"
    client.task_path.return_value = "stable-task-name"
    client.create_task.return_value.name = "stable-task-name"

    job_id = uuid.uuid4()
    incident_id = uuid.uuid4()

    first = enqueue_investigation(job_id, incident_id, client=client)
    second = enqueue_investigation(job_id, incident_id, client=client)

    assert first == second == "stable-task-name"

    first_task = client.create_task.call_args_list[0].kwargs["request"]["task"]
    second_task = client.create_task.call_args_list[1].kwargs["request"]["task"]

    assert first_task["name"] == second_task["name"]


def test_enqueue_treats_existing_task_as_success(configured_tasks):
    client = Mock(spec=tasks_v2.CloudTasksClient)
    client.queue_path.return_value = "queue-path"
    client.task_path.return_value = "existing-task-name"
    client.create_task.side_effect = AlreadyExists("already exists")

    result = enqueue_investigation(
        uuid.uuid4(),
        uuid.uuid4(),
        client=client,
    )

    assert result == "existing-task-name"


def test_enqueue_rejects_missing_configuration(monkeypatch):
    monkeypatch.setattr(settings, "gcp_project_id", "")

    with pytest.raises(RuntimeError, match="GCP_PROJECT_ID"):
        enqueue_investigation(uuid.uuid4(), uuid.uuid4())


def test_enqueue_uses_explicit_audience_when_configured(
    configured_tasks, monkeypatch
):
    explicit_audience = "https://worker.example.test"
    monkeypatch.setattr(
        settings, "investigation_worker_audience", explicit_audience
    )

    client = Mock(spec=tasks_v2.CloudTasksClient)
    client.queue_path.return_value = "queue-path"
    client.task_path.return_value = "task-path"
    client.create_task.return_value.name = "task-path"

    enqueue_investigation(uuid.uuid4(), uuid.uuid4(), client=client)

    task = client.create_task.call_args.kwargs["request"]["task"]
    assert task["http_request"]["oidc_token"]["audience"] == (
        explicit_audience
    )


@pytest.mark.parametrize(
    "missing_setting, env_name",
    [
        ("cloud_tasks_location", "CLOUD_TASKS_LOCATION"),
        ("cloud_tasks_queue", "CLOUD_TASKS_QUEUE"),
    ],
)
def test_enqueue_rejects_missing_queue_configuration(
    configured_tasks, monkeypatch, missing_setting, env_name
):
    monkeypatch.setattr(settings, missing_setting, "")

    with pytest.raises(RuntimeError, match=env_name):
        enqueue_investigation(uuid.uuid4(), uuid.uuid4())


@pytest.mark.parametrize(
    "retryable",
    [
        TooManyRequests("rate limited"),
        ServiceUnavailable("backend unavailable"),
        InternalServerError("internal error"),
        DeadlineExceeded("deadline exceeded"),
        Aborted("aborted"),
    ],
)
def test_transient_cloud_tasks_errors_are_retryable(retryable):
    assert is_retryable_error(retryable) is True


@pytest.mark.parametrize(
    "permanent",
    [
        PermissionDenied("no permission"),
        Unauthenticated("bad credentials"),
        NotFound("queue missing"),
        InvalidArgument("bad request"),
        RuntimeError("Missing Cloud Tasks configuration"),
    ],
)
def test_permanent_failures_are_not_retryable(permanent):
    assert is_retryable_error(permanent) is False


def test_effective_worker_audience_prefers_explicit_value(
    configured_tasks, monkeypatch
):
    monkeypatch.setattr(
        settings,
        "investigation_worker_audience",
        "https://worker.example.test",
    )

    assert settings.effective_worker_audience == (
        "https://worker.example.test"
    )


def test_effective_worker_audience_falls_back_to_worker_url(
    configured_tasks,
):
    assert settings.effective_worker_audience == (
        settings.investigation_worker_url
    )
