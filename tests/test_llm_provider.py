"""Tests for the OpenAI-compatible LLM provider adapter.

Fully offline: an ``httpx.MockTransport`` stands in for the provider, so
no test contacts a real API or needs a real key.
"""

import httpx
import pytest

from app.config import settings
from app.services.llm import (
    LLMConfigurationError,
    LLMResponseError,
    LLMTransientError,
    OpenAICompatibleProvider,
    build_default_provider,
    is_retryable_status,
)


def make_provider(handler, **overrides):
    """Build a provider wired to a mock transport; records slept seconds."""
    slept: list[float] = []
    provider = OpenAICompatibleProvider(
        base_url=overrides.pop("base_url", "https://llm.example.test/v1"),
        api_key=overrides.pop("api_key", "test-key-not-a-secret"),
        model=overrides.pop("model", "test-model"),
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        **overrides,
    )
    return provider, slept


def chat_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


def test_successful_completion_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key-not-a-secret"
        return httpx.Response(200, json=chat_response('{"ok": true}'))

    provider, slept = make_provider(handler)

    with provider:
        content = provider.complete_json(system="sys", user="usr")

    assert content == '{"ok": true}'
    assert slept == []


def test_transient_500_is_retried_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(200, json=chat_response('{"ok": true}'))

    provider, slept = make_provider(handler)

    with provider:
        content = provider.complete_json(system="sys", user="usr")

    assert content == '{"ok": true}'
    assert attempts["count"] == 2
    assert slept == [1]  # one bounded backoff


def test_429_is_retried():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=chat_response('{"ok": true}'))

    provider, _ = make_provider(handler)

    with provider:
        assert provider.complete_json(system="s", user="u") == '{"ok": true}'
    assert attempts["count"] == 2


def test_retries_are_bounded_and_then_raise_transient():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503, text="unavailable")

    provider, slept = make_provider(handler, max_retries=2)

    with provider, pytest.raises(LLMTransientError, match="503"):
        provider.complete_json(system="s", user="u")

    # 1 initial + 2 retries, with linear backoff between them.
    assert attempts["count"] == 3
    assert slept == [1, 2]


def test_timeout_raises_transient_error_after_bounded_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    provider, slept = make_provider(handler, max_retries=1)

    with provider, pytest.raises(LLMTransientError, match="timed out"):
        provider.complete_json(system="s", user="u")

    assert slept == [1]


def test_connection_error_raises_transient_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated refusal")

    provider, _ = make_provider(handler, max_retries=0)

    with provider, pytest.raises(LLMTransientError, match="ConnectError"):
        provider.complete_json(system="s", user="u")


def test_client_error_4xx_is_not_retried():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, text="bad key")

    provider, slept = make_provider(handler)

    with provider, pytest.raises(LLMResponseError, match="401"):
        provider.complete_json(system="s", user="u")

    assert attempts["count"] == 1
    assert slept == []


def test_malformed_response_shape_raises_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider, _ = make_provider(handler)

    with provider, pytest.raises(LLMResponseError, match="unexpected response"):
        provider.complete_json(system="s", user="u")


def test_empty_content_raises_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response("   "))

    provider, _ = make_provider(handler)

    with provider, pytest.raises(LLMResponseError, match="empty message"):
        provider.complete_json(system="s", user="u")


def test_error_messages_never_include_the_api_key():
    secret = "sk-super-secret-key-123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider, _ = make_provider(handler, api_key=secret, max_retries=0)

    with provider, pytest.raises(LLMTransientError) as excinfo:
        provider.complete_json(system="s", user="u")

    assert secret not in str(excinfo.value)


def test_is_retryable_status_classification():
    assert is_retryable_status(429) is True
    assert is_retryable_status(500) is True
    assert is_retryable_status(503) is True
    assert is_retryable_status(400) is False
    assert is_retryable_status(401) is False
    assert is_retryable_status(404) is False


def test_build_default_provider_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")

    with pytest.raises(LLMConfigurationError, match="LLM_API_KEY"):
        build_default_provider()


def test_build_default_provider_uses_settings(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "configured-key")
    monkeypatch.setattr(settings, "llm_model", "custom-model")
    monkeypatch.setattr(
        settings, "llm_base_url", "https://relay.example.test/v1/"
    )
    monkeypatch.setattr(settings, "llm_timeout_seconds", 12.5)
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    provider = build_default_provider()

    try:
        assert provider.api_key == "configured-key"
        assert provider.model == "custom-model"
        # Trailing slash is normalized so /chat/completions joins cleanly.
        assert provider.base_url == "https://relay.example.test/v1"
        assert provider.timeout_seconds == 12.5
        assert provider.max_retries == 0
    finally:
        provider.close()
