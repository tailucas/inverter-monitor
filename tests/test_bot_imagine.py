#!/usr/bin/env python
"""Unit tests for the /imagine wiring on TelegramBot (no network access)."""

from typing import Any

import pytest

from app.bot import TelegramBot
from app.image_prompts import DEFAULT_LOCATIONS, DEFAULT_STYLES, ImaginePrompt
from app.telegram_bot import BmsSummaryBuffer, SwitchStatsBuffer, WeatherBuffer

# a fixed 12-hour day for deterministic time-of-day checks
_SUNRISE = 1_000_000.0
_SUNSET = 1_043_200.0


def _sun_times(**extra: Any) -> dict:
    """A weather sample carrying the fixed daylight window."""
    return {"sunrise_epoch": _SUNRISE, "sunset_epoch": _SUNSET, **extra}


class _RecordingImageClient:
    """Image client stub that records the requests it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "4:5",
        image_size: str = "1K",
    ) -> bytes:
        self.calls.append((prompt, aspect_ratio, image_size))
        return b"image-bytes"


def _bot_stub(
    inverter_query: Any = None,
    image_client: Any = None,
) -> TelegramBot:
    """Build a TelegramBot shell without starting threads or networks."""
    bot = TelegramBot.__new__(TelegramBot)
    bot._inverter_query = inverter_query
    bot._image_client = image_client
    bot._bms_summary = BmsSummaryBuffer()
    bot._weather = WeatherBuffer()
    bot._switch_stats = SwitchStatsBuffer()
    bot._load_warning_w = 7000
    bot._load_shed_w = 7000
    return bot


def test_build_imagine_uses_live_telemetry_and_buffers() -> None:
    """The prompt reflects the live inverter plus cached weather/switches."""
    bot = _bot_stub(
        inverter_query=lambda: {
            "battery_soc_pct": 33.0,
            "battery_power_w": 900.0,
            "pv1_power_w": 0.0,
            "pv2_power_w": 0.0,
            "total_load_power_w": 8000.0,
            "grid_voltage_l1_v": 0.0,
            "alert": 0,
        }
    )
    bot._weather.update({"cloudiness_pct": 100, "midday_pct": 40})
    bot._switch_stats.update({"overcast": 1})
    request = bot.build_imagine(DEFAULT_STYLES[0], DEFAULT_LOCATIONS[0])
    assert "worried frown" in request.prompt
    assert "the sky is completely overcast" in request.prompt
    assert "load shedding active" in request.caption
    assert request.style == DEFAULT_STYLES[0]
    assert request.location == DEFAULT_LOCATIONS[0]


def test_build_imagine_survives_query_failure() -> None:
    """A failing live query still yields a usable prompt."""

    def _boom() -> dict:
        raise RuntimeError("logger down")

    bot = _bot_stub(inverter_query=_boom)
    request = bot.build_imagine(DEFAULT_STYLES[0], DEFAULT_LOCATIONS[0])
    assert "sleepy and offline" in request.prompt


def test_render_imagine_uses_style_image_format() -> None:
    """The client receives the prompt and the style's image format."""
    client = _RecordingImageClient()
    bot = _bot_stub(image_client=client)
    style = DEFAULT_STYLES[-1]
    request = ImaginePrompt(
        prompt="a prompt",
        caption="a caption",
        style=style,
        location=DEFAULT_LOCATIONS[-1],
    )
    assert bot.render_imagine(request) == b"image-bytes"
    assert client.calls == [("a prompt", style.aspect_ratio, style.image_size)]


def test_render_imagine_requires_a_client() -> None:
    """Without a configured client the command fails loudly."""
    bot = _bot_stub()
    request = ImaginePrompt(
        prompt="p",
        caption="c",
        style=DEFAULT_STYLES[0],
        location=DEFAULT_LOCATIONS[0],
    )
    with pytest.raises(RuntimeError, match="not configured"):
        bot.render_imagine(request)


def test_build_imagine_threads_material_and_time_of_day() -> None:
    """The material and the sun-relative phase reach the composed prompt."""
    bot = _bot_stub()
    bot._weather.update(_sun_times(wind_speed_ms=2.0, wind_deg=189.0))
    request = bot.build_imagine(
        DEFAULT_STYLES[0],
        DEFAULT_LOCATIONS[0],
        material="reclaimed oak",
        now=_SUNRISE + 30 * 60,
    )
    assert "made of reclaimed oak" in request.prompt
    assert "light southerly breeze" in request.prompt
    assert request.time_of_day == "dawn"
