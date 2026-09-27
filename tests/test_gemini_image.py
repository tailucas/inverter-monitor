#!/usr/bin/env python
"""Unit tests for the Gemini text-to-image client (no network access)."""

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai.errors import ClientError

from app.gemini_image import (
    DEFAULT_IMAGE_MODEL,
    IMAGE_REQUEST_TIMEOUT_SECONDS,
    GeminiImageClient,
    ImageGenerationError,
)
from app.image_prompts import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_IMAGE_SIZE,
    MAX_PROMPT_TOKENS,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-data"


class _FakeCreds:
    """Credential stub; only touched when a real genai client is built."""

    def get_creds(self, path: str) -> str:
        return "test-api-key"


class _FakeModels:
    def __init__(self, tokens: int | None = 120, error: Exception | None = None):
        self.tokens = tokens
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def count_tokens(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(total_tokens=self.tokens)


class _FakeInteractions:
    def __init__(self, interaction: Any = None, error: Exception | None = None):
        self.interaction = interaction
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.interaction


class _FakeGenaiClient:
    """Minimal stand-in for genai.Client."""

    def __init__(
        self,
        interaction: Any = None,
        tokens: int | None = 120,
        create_error: Exception | None = None,
        count_error: Exception | None = None,
    ) -> None:
        self.models = _FakeModels(tokens=tokens, error=count_error)
        self.interactions = _FakeInteractions(
            interaction=interaction, error=create_error
        )


def _interaction(
    data: Any = None,
    status: str = "completed",
    errors: Any = None,
) -> Any:
    """Build an interaction response stub with a base64 PNG payload."""
    payload = base64.b64encode(PNG_BYTES).decode() if data is None else data
    return SimpleNamespace(
        status=status,
        errors=errors,
        output_image=SimpleNamespace(data=payload, mime_type="image/png"),
    )


def _client(fake: _FakeGenaiClient, **kwargs: Any) -> GeminiImageClient:
    return GeminiImageClient(creds_obj=_FakeCreds(), client=fake, **kwargs)


def test_default_model_is_the_lite_image_model() -> None:
    """Without an override the client targets the default image model."""
    client = _client(_FakeGenaiClient(interaction=_interaction()))
    assert client.model == DEFAULT_IMAGE_MODEL


def test_model_is_configurable() -> None:
    """A model override is honoured."""
    client = _client(_FakeGenaiClient(interaction=_interaction()), model="pro-image")
    assert client.model == "pro-image"


def test_generate_image_decodes_base64_payload() -> None:
    """The interaction payload is decoded into raw image bytes."""
    fake = _FakeGenaiClient(interaction=_interaction())
    assert _client(fake).generate_image("a prompt") == PNG_BYTES


def test_generate_image_passes_mobile_response_format() -> None:
    """Portrait aspect ratio and 1K size ride along in the request."""
    fake = _FakeGenaiClient(interaction=_interaction())
    _client(fake).generate_image("a prompt")
    call = fake.interactions.calls[0]
    assert call["model"] == DEFAULT_IMAGE_MODEL
    assert call["input"] == "a prompt"
    assert call["response_format"] == {
        "type": "image",
        "aspect_ratio": DEFAULT_ASPECT_RATIO,
        "image_size": DEFAULT_IMAGE_SIZE,
    }
    assert call["timeout"] == IMAGE_REQUEST_TIMEOUT_SECONDS


def test_generate_image_honours_scene_aspect_ratio() -> None:
    """A tall scene overrides the default portrait ratio."""
    fake = _FakeGenaiClient(interaction=_interaction())
    _client(fake).generate_image("a prompt", aspect_ratio="9:16")
    response_format = fake.interactions.calls[0]["response_format"]
    assert response_format["aspect_ratio"] == "9:16"


def test_generate_image_counts_the_prompt_first() -> None:
    """The token count is measured against the configured model."""
    fake = _FakeGenaiClient(interaction=_interaction())
    _client(fake).generate_image("a prompt")
    assert fake.models.calls == [{"model": DEFAULT_IMAGE_MODEL, "contents": "a prompt"}]


def test_generate_image_rejects_over_budget_prompt() -> None:
    """An over-long prompt never reaches the image model."""
    over_budget = MAX_PROMPT_TOKENS + 1
    fake = _FakeGenaiClient(interaction=_interaction(), tokens=over_budget)
    with pytest.raises(ImageGenerationError, match=str(over_budget)):
        _client(fake).generate_image("a prompt")
    assert fake.interactions.calls == []


def test_generate_image_falls_back_when_count_fails() -> None:
    """A failed token count degrades to the character estimate."""
    fake = _FakeGenaiClient(
        interaction=_interaction(),
        count_error=RuntimeError("count unavailable"),
    )
    assert _client(fake).generate_image("short prompt") == PNG_BYTES
    assert len(fake.interactions.calls) == 1


def test_generate_image_surfaces_client_error_message() -> None:
    """The client's own error message is relayed to the caller."""
    error = ClientError(
        400,
        {
            "error": {
                "code": 400,
                "message": "The prompt is too long: 512 tokens (max 700).",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    fake = _FakeGenaiClient(create_error=error)
    with pytest.raises(ImageGenerationError, match="The prompt is too long"):
        _client(fake).generate_image("a prompt")


class _GaosStyleError(Exception):
    """Stand-in for the Interactions client's GAOS compat error classes."""

    def __init__(self, body: object, message: str = "verbose error dump") -> None:
        super().__init__(message)
        self.status_code = 400
        self.message = message
        self.body = body


def test_generate_image_extracts_message_from_interactions_error() -> None:
    """The Interactions error body yields the clean server message."""
    error = _GaosStyleError(
        [
            {
                "error": {
                    "code": 400,
                    "message": "The prompt is too long: 512 tokens (max 700).",
                    "status": "INVALID_ARGUMENT",
                }
            }
        ],
        message="Error code: 400 - [{...}]",
    )
    fake = _FakeGenaiClient(create_error=error)
    with pytest.raises(ImageGenerationError, match="The prompt is too long"):
        _client(fake).generate_image("a prompt")


def test_generate_image_extracts_message_from_dict_body() -> None:
    """A dict error body is parsed the same way as a list body."""
    error = _GaosStyleError(
        {"error": {"message": "quota exceeded"}}, message="Error code: 429"
    )
    fake = _FakeGenaiClient(create_error=error)
    with pytest.raises(ImageGenerationError, match="quota exceeded"):
        _client(fake).generate_image("a prompt")


def test_generate_image_surfaces_unexpected_errors() -> None:
    """Errors without a status code still surface their own text."""
    fake = _FakeGenaiClient(create_error=RuntimeError("connection reset"))
    with pytest.raises(ImageGenerationError, match="connection reset"):
        _client(fake).generate_image("a prompt")


def test_generate_image_reports_failed_interaction() -> None:
    """A failed interaction surfaces its error message."""
    interaction = _interaction(
        status="failed",
        errors=[SimpleNamespace(code="INVALID_ARGUMENT", message="prompt rejected")],
    )
    fake = _FakeGenaiClient(interaction=interaction)
    with pytest.raises(ImageGenerationError, match="prompt rejected"):
        _client(fake).generate_image("a prompt")


def test_generate_image_requires_an_image() -> None:
    """A response without an image is an error."""
    interaction = SimpleNamespace(status="completed", errors=None, output_image=None)
    fake = _FakeGenaiClient(interaction=interaction)
    with pytest.raises(ImageGenerationError, match="no image"):
        _client(fake).generate_image("a prompt")


def test_generate_image_rejects_empty_payload() -> None:
    """An empty payload is treated as a missing image."""
    fake = _FakeGenaiClient(interaction=_interaction(data=""))
    with pytest.raises(ImageGenerationError, match="no image"):
        _client(fake).generate_image("a prompt")


def test_generate_image_accepts_raw_bytes_payload() -> None:
    """Raw bytes from the SDK validator pass straight through."""
    fake = _FakeGenaiClient(interaction=_interaction(data=PNG_BYTES))
    assert _client(fake).generate_image("a prompt") == PNG_BYTES


def test_generate_image_raises_unreadable_payload() -> None:
    """Malformed base64 is reported as an unreadable image."""
    fake = _FakeGenaiClient(interaction=_interaction(data="not-base64!!!"))
    with pytest.raises(ImageGenerationError, match="unreadable"):
        _client(fake).generate_image("a prompt")
