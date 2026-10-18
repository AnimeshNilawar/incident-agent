"""Provider-agnostic LLM access for the investigation engine.

The engine depends only on the tiny ``LLMProvider`` protocol; the one
shipped adapter speaks the OpenAI-compatible chat-completions protocol
over plain HTTP (``httpx``, already a project dependency), so any
compatible server can be used by changing ``LLM_BASE_URL`` / ``LLM_MODEL``
- no provider SDK, no second adapter.

Operational rules encoded here:

* Every request carries a hard timeout; nothing can hang a worker.
* Transient failures (timeouts, connection errors, 429, 5xx) are retried
  a bounded number of times with linear backoff. Everything else (4xx,
  malformed responses) fails immediately - retrying cannot help.
* Error messages carry the HTTP status and exception *type* only. They
  never include the API key, request headers, or the full request, and
  the response body is truncated so a provider error page cannot leak
  into logs unbounded.
"""

import logging
import time
from typing import Protocol, Self

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Truncation for provider error bodies echoed into exceptions. Short
# enough for logs, long enough to diagnose.
MAX_ERROR_BODY_CHARS = 300


class LLMError(RuntimeError):
    """Base class for LLM provider failures."""


class LLMConfigurationError(LLMError):
    """The provider is not configured (missing URL or API key)."""


class LLMTransientError(LLMError):
    """A retryable failure: timeout, connection error, 429, or 5xx."""


class LLMResponseError(LLMError):
    """A non-retryable failure: the provider rejected or mangled the request."""


class LLMProvider(Protocol):
    """What the investigation engine needs from a model provider."""

    def complete_json(self, *, system: str, user: str) -> str:
        """Send a chat request and return the model's raw text output.

        Output is expected to be a JSON object; parsing and validation
        happen in the engine, not here. Raises ``LLMError`` subclasses.
        """
        ...


def is_retryable_status(status_code: int) -> bool:
    """HTTP statuses worth a bounded retry."""
    return status_code == 429 or status_code >= 500


class OpenAICompatibleProvider:
    """Minimal adapter for OpenAI-compatible chat-completions endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        # An injectable transport keeps tests offline and deterministic.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def complete_json(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # JSON mode: the model is constrained to emit a JSON object,
            # which the engine then strictly validates.
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error: LLMError | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(
                    "/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                last_error = LLMTransientError(
                    f"LLM request timed out after {self.timeout_seconds}s "
                    f"({type(exc).__name__})"
                )
            except httpx.HTTPError as exc:
                last_error = LLMTransientError(
                    f"LLM request failed ({type(exc).__name__})"
                )
            else:
                if response.is_success:
                    return self._extract_content(response)

                body = response.text[:MAX_ERROR_BODY_CHARS]

                if is_retryable_status(response.status_code):
                    last_error = LLMTransientError(
                        f"LLM provider returned HTTP "
                        f"{response.status_code}: {body}"
                    )
                else:
                    raise LLMResponseError(
                        f"LLM provider rejected the request with HTTP "
                        f"{response.status_code}: {body}"
                    ) from None

            if attempt < self.max_retries:
                # Linear backoff: 1s, 2s, ... bounded by max_retries.
                self._sleep(attempt + 1)

        # Unreachable in practice: every loop iteration either returns,
        # raises, or assigns ``last_error``. Kept total for type-checkers.
        raise last_error or LLMTransientError("LLM request failed")

    def _extract_content(self, response: httpx.Response) -> str:
        """Pull the assistant message text out of a chat-completions body.

        A structurally unexpected body is a provider bug, not something
        to retry, so it raises ``LLMResponseError`` immediately.
        """
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                "LLM provider returned an unexpected response shape "
                f"({type(exc).__name__})"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError(
                "LLM provider returned an empty message content"
            )

        return content


def build_default_provider() -> OpenAICompatibleProvider:
    """Build the configured provider from environment settings.

    Raises ``LLMConfigurationError`` when the credential is missing: the
    engine must fail the job with an explicit reason rather than run the
    model anonymously or, worse, fabricate an ungrounded report.
    """
    if not settings.llm_configured:
        raise LLMConfigurationError(
            "LLM provider is not configured (LLM_API_KEY is empty)"
        )

    # The key is deliberately never logged; only the non-secret target.
    logger.debug(
        "Building LLM provider: model=%s base_url=%s",
        settings.llm_model,
        settings.llm_base_url,
    )

    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
