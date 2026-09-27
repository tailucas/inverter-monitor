#!/usr/bin/env python
"""Gemini text-to-image client backing the Telegram /imagine command.

Wraps the google-genai Interactions API: the prompt is counted against the
project's self-imposed prompt budget before the request is sent, the
returned base64 image payload is decoded, and every failure is raised as
``ImageGenerationError`` carrying the client's own error message so the bot
can relay it to the user. The transient ``404 Requested entity was not
found`` the Interactions API occasionally returns for a valid request is
retried once before the failure is reported.

No Telegram imports and no asyncio: the bot thread calls these blocking
helpers through an executor, and the image response format is taken from the
selected scene configuration. Unit-tested in ``tests/test_gemini_image.py``.
"""

import base64
import time
from typing import Any

from google import genai
from opentelemetry import metrics
from tailucas_pylib import APP_NAME, app_config, log

from app.image_prompts import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_IMAGE_SIZE,
    MAX_PROMPT_TOKENS,
    estimate_prompt_tokens,
)

DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-lite-image"
# bounded time for one text-to-image round trip
IMAGE_REQUEST_TIMEOUT_SECONDS = 120
# the Interactions API intermittently answers a valid image request with
# "404 Requested entity was not found"; one delayed retry clears it
NOT_FOUND_STATUS_CODE = 404
IMAGE_REQUEST_RETRY_DELAY_SECONDS = 1.0

OTEL_METER = metrics.get_meter(APP_NAME)
IMAGE_GENERATION_DURATION = OTEL_METER.create_gauge(
    name="gemini_image_duration_seconds",
    description="Gemini text-to-image round-trip duration",
)


class ImageGenerationError(RuntimeError):
    """Raised when an image cannot be generated.

    Carries the message returned by the Gemini client so the bot can relay
    it to the user verbatim.
    """


def _message_from_body(body: Any) -> str | None:
    """Pull the ``error.message`` field out of a JSON error body."""
    payload = body
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


def _server_error_message(exc: Exception) -> str:
    """Extract the API's own message from either GenAI error hierarchy.

    The classic client raises ``google.genai.errors`` subclasses whose
    ``message`` is already clean, while the Interactions client raises
    ``_gaos`` compatibility errors that carry the response ``body``.
    """
    candidates = (
        _message_from_body(getattr(exc, "body", None)),
        getattr(exc, "message", None),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(exc)


def _status_code(exc: Exception) -> int | None:
    """Extract an HTTP status code from either GenAI error hierarchy."""
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _is_retryable_not_found(exc: Exception) -> bool:
    """Detect the intermittent "entity not found" failure of the API.

    A valid image request is occasionally answered with
    ``404 Requested entity was not found``; the same request succeeds when
    repeated, so a single retry is warranted.
    """
    if _status_code(exc) != NOT_FOUND_STATUS_CODE:
        return False
    return "not found" in _server_error_message(exc).lower()


class GeminiImageClient:
    """Blocking text-to-image client for the configured Gemini image model."""

    def __init__(
        self,
        creds_obj: Any,
        model: str | None = None,
        client: Any = None,
        max_prompt_tokens: int = MAX_PROMPT_TOKENS,
    ) -> None:
        self.model = model or app_config.get(
            "gemini", "model", fallback=DEFAULT_IMAGE_MODEL
        )
        self.max_prompt_tokens = max_prompt_tokens
        self._client = client
        if self._client is None:
            api_key = creds_obj.get_creds(f"Gemini/{APP_NAME}/token")
            self._client = genai.Client(api_key=api_key)
        log.info(
            "Gemini image client configured",
            extra={"model": self.model, "prompt_token_limit": max_prompt_tokens},
        )

    def _count_prompt_tokens(self, prompt: str) -> int:
        """Count prompt tokens, falling back to a character estimate."""
        try:
            response = self._client.models.count_tokens(
                model=self.model, contents=prompt
            )
            tokens = int(response.total_tokens or 0)
            log.debug(
                "Counted image prompt tokens",
                extra={"model": self.model, "prompt_tokens": tokens},
            )
            return tokens
        except Exception:
            estimate = estimate_prompt_tokens(prompt)
            log.warning(
                "Prompt token count failed; using character estimate",
                exc_info=True,
                extra={
                    "model": self.model,
                    "prompt_chars": len(prompt),
                    "prompt_tokens_estimate": estimate,
                },
            )
            return estimate

    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        image_size: str = DEFAULT_IMAGE_SIZE,
    ) -> bytes:
        """Generate an image and return the decoded image bytes.

        Raises:
            ImageGenerationError: when the prompt is over the model's prompt
                budget or the client cannot produce an image.
        """
        started = time.time()
        try:
            return self._generate_image(prompt, aspect_ratio, image_size)
        finally:
            IMAGE_GENERATION_DURATION.set(time.time() - started)

    def _generate_image(self, prompt: str, aspect_ratio: str, image_size: str) -> bytes:
        prompt_tokens = self._count_prompt_tokens(prompt)
        if prompt_tokens > self.max_prompt_tokens:
            log.warning(
                "Rejecting over-long image prompt",
                extra={
                    "model": self.model,
                    "prompt_chars": len(prompt),
                    "prompt_tokens": prompt_tokens,
                    "prompt_token_limit": self.max_prompt_tokens,
                },
            )
            raise ImageGenerationError(
                f"the prompt is {prompt_tokens} tokens, over the "
                f"{self.max_prompt_tokens}-token limit for {self.model}"
            )
        response_format = {
            "type": "image",
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        }
        log.info(
            "Requesting Gemini image",
            extra={
                "model": self.model,
                "prompt_chars": len(prompt),
                "prompt_tokens": prompt_tokens,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
            },
        )
        interaction = self._request_interaction(prompt, response_format)
        self._raise_for_interaction_errors(interaction)
        image_bytes = self._decode_output_image(interaction)
        log.info(
            "Gemini image generated",
            extra={
                "model": self.model,
                "prompt_tokens": prompt_tokens,
                "image_bytes": len(image_bytes),
                "aspect_ratio": aspect_ratio,
            },
        )
        return image_bytes

    def _request_interaction(self, prompt: str, response_format: dict[str, str]) -> Any:
        """Create the interaction, retrying a transient "not found" once.

        The Interactions API intermittently rejects a valid request with
        ``404 Requested entity was not found``; one delayed retry clears it.
        The retry's outcome is reported exactly like a first failure: the
        warning is logged and ``ImageGenerationError`` carries the API's own
        message back to the user.
        """
        try:
            return self._create_interaction(prompt, response_format)
        except Exception as exc:
            if not _is_retryable_not_found(exc):
                raise self._image_request_error(exc) from exc
            log.info(
                "Retrying Gemini image request after not-found error",
                extra={
                    "model": self.model,
                    "error": _server_error_message(exc),
                    "status_code": _status_code(exc),
                    "retry_delay_seconds": IMAGE_REQUEST_RETRY_DELAY_SECONDS,
                },
            )
            time.sleep(IMAGE_REQUEST_RETRY_DELAY_SECONDS)
        try:
            return self._create_interaction(prompt, response_format)
        except Exception as exc:
            raise self._image_request_error(exc) from exc

    def _create_interaction(self, prompt: str, response_format: dict[str, str]) -> Any:
        """Send one image request to the Interactions API."""
        return self._client.interactions.create(
            model=self.model,
            input=prompt,
            response_format=response_format,
            timeout=IMAGE_REQUEST_TIMEOUT_SECONDS,
        )

    def _image_request_error(self, exc: Exception) -> ImageGenerationError:
        """Log a failed image request and build the error to relay."""
        status_code = _status_code(exc)
        error_message = _server_error_message(exc)
        if status_code is not None:
            log.warning(
                "Gemini image request rejected",
                extra={
                    "model": self.model,
                    "error": error_message,
                    "status_code": status_code,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            log.warning(
                "Unexpected error requesting Gemini image",
                exc_info=True,
                extra={"model": self.model, "error": error_message},
            )
        return ImageGenerationError(error_message)

    @staticmethod
    def _raise_for_interaction_errors(interaction: Any) -> None:
        """Raise when the interaction reports failed steps."""
        details: list[str] = []
        for error in getattr(interaction, "errors", None) or []:
            parts = [
                str(part)
                for part in (
                    getattr(error, "code", None),
                    getattr(error, "message", None),
                )
                if part
            ]
            if parts:
                details.append(": ".join(parts))
        if details:
            raise ImageGenerationError("; ".join(details))
        if getattr(interaction, "status", None) == "failed":
            raise ImageGenerationError("the image request failed")

    @staticmethod
    def _decode_output_image(interaction: Any) -> bytes:
        """Decode the base64 image payload from an interaction response."""
        image = getattr(interaction, "output_image", None)
        data = getattr(image, "data", None) if image is not None else None
        if not data:
            raise ImageGenerationError("the model returned no image")
        if isinstance(data, bytes):
            # the SDK validator passes raw bytes through when it built the
            # payload locally; server responses arrive base64-encoded
            image_bytes = data
        else:
            try:
                image_bytes = base64.b64decode(data)
            except (ValueError, TypeError) as exc:
                raise ImageGenerationError(
                    "the model returned an unreadable image"
                ) from exc
        if not image_bytes:
            raise ImageGenerationError("the model returned an empty image")
        return image_bytes
