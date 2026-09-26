#!/usr/bin/env python
"""Unit tests for the pure image-prompt builder."""

import random

import pytest

from app.image_prompts import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_SCENES,
    MAX_PROMPT_CHARS,
    MAX_PROMPT_TOKENS,
    SceneConfig,
    build_image_prompt,
    estimate_prompt_tokens,
    find_scene,
    load_shed_state,
    numeric_value,
    scene_names,
    select_scene,
)


def _healthy_inverter() -> dict:
    """A sunny, well-charged inverter sample."""
    return {
        "battery_soc_pct": 82.0,
        "battery_voltage_v": 51.2,
        "battery_power_w": -850.0,
        "pv1_power_w": 1200.0,
        "pv2_power_w": 800.0,
        "total_load_power_w": 950.0,
        "grid_voltage_l1_v": 230.5,
        "grid_voltage_l2_v": 0.0,
        "alert": 0,
    }


def _worst_case_inverter() -> dict:
    """An alarming sample with every visual cue switched on."""
    return {
        "battery_soc_pct": 12.9,
        "battery_voltage_v": 44.4,
        "battery_power_w": 5432.1,
        "pv1_power_w": 0.0,
        "pv2_power_w": 0.0,
        "total_load_power_w": 99999.9,
        "grid_voltage_l1_v": 0.0,
        "grid_voltage_l2_v": 0.0,
        "alert": 1,
    }


def test_prompt_describes_centered_inverter_with_weather_above() -> None:
    """The mandatory layout: inverter centred, weather in the upper frame."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        weather={"cloudiness_pct": 100, "midday_pct": 45},
    )
    assert "middle of the frame" in prompt
    assert "Above the inverter the upper part of the frame" in prompt
    assert "grey cloud" in prompt
    assert "load shedding is not needed" in prompt


def test_prompt_sky_handles_night_and_overcast() -> None:
    """Night and daytime weather produce distinct sky descriptions."""
    night = build_image_prompt(
        inverter=_healthy_inverter(),
        weather={"cloudiness_pct": 0, "midday_pct": 0},
    )
    assert "night sky pricked with stars" in night
    overcast_night = build_image_prompt(
        inverter=_healthy_inverter(),
        weather={"cloudiness_pct": 100, "midday_pct": 0},
    )
    assert "moon hidden behind thick cloud" in overcast_night


def test_prompt_face_reflects_healthy_battery() -> None:
    """A charging battery with a high reserve produces a happy face."""
    prompt = build_image_prompt(inverter=_healthy_inverter())
    assert "cheerful smile" in prompt
    assert "green status light" in prompt


def test_prompt_face_reflects_alert_and_low_battery() -> None:
    """An alert or a critically low reserve produces a worried face."""
    inverter = _healthy_inverter() | {"alert": 1, "battery_soc_pct": 28.0}
    prompt = build_image_prompt(inverter=inverter)
    assert "worried frown" in prompt
    assert "red status light" in prompt


def test_prompt_face_reflects_heavy_draw() -> None:
    """A heavy discharge produces a strained face."""
    inverter = _healthy_inverter() | {
        "battery_power_w": 900.0,
        "battery_soc_pct": 70.0,
    }
    prompt = build_image_prompt(inverter=inverter)
    assert "strained" in prompt
    assert "amber status light" in prompt


def test_prompt_offline_inverter_is_sleepy() -> None:
    """No live sample still yields a face that reflects the outage."""
    prompt = build_image_prompt(inverter=None)
    assert "sleepy and offline" in prompt


def test_prompt_indicates_load_shedding_needed() -> None:
    """An active latch puts the shedding indicator into the prompt."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter() | {"total_load_power_w": 8100.0},
        switches={"load_shed": 1},
    )
    assert "load shedding is needed" in prompt
    assert "crossed-out plug" in prompt
    assert "the household load is too high" in prompt


def test_prompt_no_load_shedding_when_load_is_low() -> None:
    """A calm system states that shedding is not needed."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        switches={"load_shed": 0, "overcast": 0},
    )
    assert "load shedding is not needed" in prompt
    assert "crossed-out plug" not in prompt


def test_prompt_uses_overcast_reason_when_cloudy() -> None:
    """The overcast latch wins over the live load reading."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        weather={"cloudiness_pct": 100, "midday_pct": 45},
        switches={"overcast": 1},
    )
    assert "load shedding is needed" in prompt
    assert "the sky is completely overcast" in prompt


def test_prompt_reports_cell_imbalance_detail() -> None:
    """A wide cell spread is visible on the battery badge."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        bms={"cell_diff_mv": 120.0, "active_count": 3},
    )
    assert "imbalance" in prompt
    assert "3 battery packs" in prompt


def test_prompt_reports_solar_and_grid_conditions() -> None:
    """Solar output and grid presence drive the prop sentences."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter()
        | {"grid_voltage_l1_v": 0.0, "pv1_power_w": 0.0, "pv2_power_w": 0.0}
    )
    assert "rest dim and still" in prompt
    assert "dark and disconnected" in prompt


def test_default_scenes_are_portrait_and_lite_sized() -> None:
    """Every default scene targets a mobile-portrait image at 1K."""
    portrait = {"3:4", "4:5", "9:16"}
    assert DEFAULT_SCENES
    for scene in DEFAULT_SCENES:
        assert scene.aspect_ratio in portrait
        assert scene.image_size == "1K"
    assert DEFAULT_SCENES[0].aspect_ratio == DEFAULT_ASPECT_RATIO


@pytest.mark.parametrize("scene", DEFAULT_SCENES, ids=scene_names())
def test_prompt_includes_all_scene_ingredients(scene: SceneConfig) -> None:
    """Setting, style, lighting, camera, lens and palette are all woven in."""
    prompt = build_image_prompt(inverter=_healthy_inverter(), scene=scene)
    assert scene.setting in prompt
    assert scene.style in prompt
    assert scene.lighting in prompt
    assert scene.camera in prompt
    assert scene.lens in prompt
    assert scene.palette is not None and scene.palette in prompt


@pytest.mark.parametrize("scene", DEFAULT_SCENES, ids=scene_names())
def test_prompt_stays_in_budget_for_every_scene(scene: SceneConfig) -> None:
    """Even the worst-case sample stays inside the 480-token budget."""
    prompt = build_image_prompt(
        inverter=_worst_case_inverter(),
        bms={"active_count": 8, "cell_diff_mv": 250.0},
        weather={"cloudiness_pct": 100, "midday_pct": 0},
        switches={"load_shed": 1},
        scene=scene,
    )
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert estimate_prompt_tokens(prompt) <= MAX_PROMPT_TOKENS
    assert len(prompt.split()) <= 400
    assert scene.aspect_ratio in prompt


def test_prompt_avoids_negative_instructions() -> None:
    """Absence is framed positively, never as a negative instruction."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        weather={"cloudiness_pct": 0, "midday_pct": 60},
    ).lower()
    assert "no text" not in prompt
    assert "no watermark" not in prompt
    assert "do not " not in prompt


def test_prompt_handles_missing_telemetry() -> None:
    """The builder always produces a prompt, even with no data at all."""
    prompt = build_image_prompt()
    assert "middle of the frame" in prompt
    assert "sleepy and offline" in prompt
    assert "load shedding is not needed" in prompt
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_prompt_handles_junk_values() -> None:
    """Non-numeric telemetry is ignored rather than propagated."""
    prompt = build_image_prompt(
        inverter={
            "battery_soc_pct": "junk",
            "alert": "maybe",
            "total_load_power_w": None,
        },
        bms={"cell_diff_mv": "x"},
        weather={"cloudiness_pct": "overcast", "midday_pct": ""},
    )
    assert "middle of the frame" in prompt
    assert "neutral sky" in prompt


def test_find_scene_is_case_insensitive() -> None:
    """Scene names match regardless of case and padding."""
    scene = find_scene(DEFAULT_SCENES, "  ORBITAL_STATION ")
    assert scene is not None
    assert scene.name == "orbital_station"


def test_find_scene_unknown_returns_none() -> None:
    """An unknown scene name resolves to None."""
    assert find_scene(DEFAULT_SCENES, "moon_base") is None


def test_select_scene_is_deterministic_with_seeded_rng() -> None:
    """Random scene selection can be pinned by a seeded generator."""
    first = select_scene(DEFAULT_SCENES, random.Random(7))
    second = select_scene(DEFAULT_SCENES, random.Random(7))
    assert first == second
    assert first in DEFAULT_SCENES


def test_select_scene_requires_a_scene() -> None:
    """An empty scene list is a programming error."""
    with pytest.raises(ValueError):
        select_scene(tuple())


def test_load_shed_state_prefers_switch_latches() -> None:
    """Switch reasons outrank the live load reading."""
    state = load_shed_state(
        _healthy_inverter(),
        {"load_shed": 0, "overcast": 1},
    )
    assert state.needed is True
    assert state.reason == "the sky is completely overcast"


def test_load_shed_state_falls_back_to_live_load() -> None:
    """Without a switch latch the load threshold decides."""
    state = load_shed_state(
        _healthy_inverter() | {"total_load_power_w": 7500.0},
        None,
        load_warning_w=7000.0,
        load_shed_w=7000.0,
    )
    assert state.needed is True


def test_load_shed_state_is_false_without_pressure() -> None:
    """A quiet system reports no shedding."""
    state = load_shed_state(_healthy_inverter(), {"load_shed": 0})
    assert state.needed is False
    assert state.reason is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (None, None),
        ("junk", None),
        ("", None),
        (12, 12.0),
        ("12.5", 12.5),
        (3.5, 3.5),
    ],
)
def test_numeric_value_coercion(value: object, expected: float | None) -> None:
    """Only genuine numbers (or numeric strings) are accepted."""
    assert numeric_value(value) == expected


def test_estimate_prompt_tokens_rounds_up() -> None:
    """The estimate never under-reports a short prompt."""
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("abc") == 1
    assert estimate_prompt_tokens("abcd") == 1
    assert estimate_prompt_tokens("abcde") == 2
