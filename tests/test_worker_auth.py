"""Focused tests for the Cloud Tasks OIDC verification path.

These cover the authentication decision itself (fail-closed configuration,
signature/audience verification ordering, and identity checks) by patching
``google.oauth2.id_token.verify_oauth2_token``.

What is deliberately NOT covered here: verifying a genuinely Google-issued
token signature, because producing one requires a real Google service
account and a live call to Google's token endpoints. The library performs
signature, issuer, audience, and expiry verification in a single call
(``verify_oauth2_token``), so those guarantees are exercised by the real
call in production, not by a locally forgeable token. Tests here therefore
assert that:

* the verified token is validated *before* any claim is trusted,
* the exact audience and token are handed to the verifier,
* and every failure mode maps to a non-2xx response (401/403/503) so Cloud
  Tasks never receives a success for an unauthenticated request.
"""

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api.worker import verify_cloud_tasks_request
from app.config import settings

SERVICE_ACCOUNT = "tasks-invoker@test-project.iam.gserviceaccount.com"
WORKER_URL = "https://worker.example.test/api/v1/worker/investigate"
EXPLICIT_AUDIENCE = "https://worker.example.test"
BEARER = "Bearer test-token"


@pytest.fixture
def worker_settings(monkeypatch):
    monkeypatch.setattr(
        settings, "cloud_tasks_service_account_email", SERVICE_ACCOUNT
    )
    monkeypatch.setattr(settings, "investigation_worker_url", WORKER_URL)
    monkeypatch.setattr(settings, "investigation_worker_audience", "")


def patch_verifier(monkeypatch, **kwargs):
    verifier = Mock(**kwargs)
    monkeypatch.setattr(
        "app.api.worker.id_token.verify_oauth2_token", verifier
    )
    return verifier


def test_rejects_missing_authorization_header(worker_settings):
    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=None)

    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def test_rejects_non_bearer_scheme(worker_settings):
    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization="Token test-token")

    assert exc.value.status_code == 401


def test_rejects_empty_bearer_token(worker_settings, monkeypatch):
    verifier = patch_verifier(monkeypatch, side_effect=ValueError())

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization="Bearer ")

    assert exc.value.status_code == 401
    verifier.assert_called_once()


def test_rejects_invalid_or_expired_token(worker_settings, monkeypatch):
    patch_verifier(monkeypatch, side_effect=ValueError("Token expired"))

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 401


def test_rejects_token_issued_for_a_different_audience(
    worker_settings, monkeypatch
):
    # An ``aud`` mismatch makes the library raise before it returns any
    # claim, so a token minted for another service never authenticates here.
    patch_verifier(
        monkeypatch, side_effect=ValueError("Token audience does not match")
    )

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 401


def test_fails_closed_when_token_cannot_be_verified(
    worker_settings, monkeypatch
):
    # A transport/verification failure must not fall through to trusting the
    # claims; it is reported as "unable to verify", never as success.
    patch_verifier(
        monkeypatch, side_effect=RuntimeError("certificate fetch failed")
    )

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 503


def test_requires_service_account_configuration(monkeypatch):
    monkeypatch.setattr(
        settings, "investigation_worker_url", WORKER_URL
    )
    monkeypatch.setattr(settings, "cloud_tasks_service_account_email", "")
    monkeypatch.setattr(settings, "investigation_worker_audience", "")

    verifier = patch_verifier(monkeypatch, return_value={})

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 503
    verifier.assert_not_called()


def test_requires_audience_configuration(monkeypatch):
    monkeypatch.setattr(
        settings, "cloud_tasks_service_account_email", SERVICE_ACCOUNT
    )
    monkeypatch.setattr(settings, "investigation_worker_url", "")
    monkeypatch.setattr(settings, "investigation_worker_audience", "")

    verifier = patch_verifier(monkeypatch, return_value={})

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 503
    verifier.assert_not_called()


def test_rejects_unexpected_service_account(worker_settings, monkeypatch):
    patch_verifier(
        monkeypatch,
        return_value={
            "email": "someone-else@test-project.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 403


@pytest.mark.parametrize("email_verified", [False, "true", None])
def test_rejects_unverified_email_claim(
    worker_settings, monkeypatch, email_verified
):
    patch_verifier(
        monkeypatch,
        return_value={
            "email": SERVICE_ACCOUNT,
            "email_verified": email_verified,
        },
    )

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 403


def test_rejects_missing_email_claim(worker_settings, monkeypatch):
    patch_verifier(monkeypatch, return_value={"email_verified": True})

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 403


def test_accepts_verified_service_account_token(
    worker_settings, monkeypatch
):
    verifier = patch_verifier(
        monkeypatch,
        return_value={
            "email": SERVICE_ACCOUNT,
            "email_verified": True,
        },
    )

    assert verify_cloud_tasks_request(authorization=BEARER) is None

    args, kwargs = verifier.call_args
    assert args[0] == "test-token"
    assert kwargs["audience"] == WORKER_URL


def test_uses_explicit_audience_when_configured(
    worker_settings, monkeypatch
):
    monkeypatch.setattr(
        settings, "investigation_worker_audience", EXPLICIT_AUDIENCE
    )

    verifier = patch_verifier(
        monkeypatch,
        return_value={
            "email": SERVICE_ACCOUNT,
            "email_verified": True,
        },
    )

    verify_cloud_tasks_request(authorization=BEARER)

    assert verifier.call_args.kwargs["audience"] == EXPLICIT_AUDIENCE


def test_verification_precedes_identity_check(worker_settings, monkeypatch):
    # Even with a token that *claims* the expected service account, an
    # unverifiable signature must fail closed before any claim is read.
    patch_verifier(monkeypatch, side_effect=ValueError("bad signature"))

    with pytest.raises(HTTPException) as exc:
        verify_cloud_tasks_request(authorization=BEARER)

    assert exc.value.status_code == 401
