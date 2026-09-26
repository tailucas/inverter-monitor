#!/usr/bin/env python
"""Unit tests for the /imagine wiring on TelegramBot (no network access)."""

from typing import Any

import pytest

from app.bot import TelegramBot
from app.image_prompts import DEFAULT_SCENES, ImaginePrompt
from app.telegram_bot import BmsSummaryBuffer, SwitchStatsBuffer, WeatherBuffer


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
    request = bot.build_imagine(DEFAULT_SCENES[0])
    assert "worried frown" in request.prompt
    assert "the sky is completely overcast" in request.prompt
    assert "load shedding active" in request.caption
    assert request.scene == DEFAULT_SCENES[0]


def test_build_imagine_survives_query_failure() -> None:
    """A failing live query still yields a usable prompt."""

    def _boom() -> dict:
        raise RuntimeError("logger down")

    bot = _bot_stub(inverter_query=_boom)
    request = bot.build_imagine(DEFAULT_SCENES[0])
    assert "sleepy and offline" in request.prompt


def test_render_imagine_uses_scene_image_format() -> None:
    """The client receives the prompt and the scene's image format."""
    client = _RecordingImageClient()
    bot = _bot_stub(image_client=client)
    scene = DEFAULT_SCENES[-1]
    request = ImaginePrompt(prompt="a prompt", caption="a caption", scene=scene)
    assert bot.render_imagine(request) == b"image-bytes"
    assert client.calls == [("a prompt", scene.aspect_ratio, scene.image_size)]


def test_render_imagine_requires_a_client() -> None:
    """Without a configured client the command fails loudly."""
    bot = _bot_stub()
    request = ImaginePrompt(prompt="p", caption="c", scene=DEFAULT_SCENES[0])
    with pytest.raises(RuntimeError, match="not configured"):
        bot.render_imagine(request)


def test_resolve_scene_by_name_and_random_fallback() -> None:
    """Named scenes resolve exactly; no argument picks a random scene."""
    bot = _bot_stub()
    named = bot.resolve_scene(["ORBITAL_STATION"])
    assert named is not None and named.name == "orbital_station"
    assert bot.resolve_scene(None) in DEFAULT_SCENES
    assert bot.resolve_scene(["nope"]) is None
