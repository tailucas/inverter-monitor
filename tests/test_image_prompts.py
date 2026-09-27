#!/usr/bin/env python
"""Unit tests for the pure image-prompt builder."""

import random

import pytest

from app.image_prompts import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_LOCATIONS,
    DEFAULT_STYLES,
    MAX_MATERIAL_CHARS,
    MAX_PROMPT_CHARS,
    MAX_PROMPT_TOKENS,
    TIME_OF_DAY_PHASES,
    LocationConfig,
    StyleConfig,
    build_image_prompt,
    estimate_prompt_tokens,
    find_style,
    load_shed_state,
    location_names,
    numeric_value,
    resolve_imagine_args,
    sanitize_material,
    select_location,
    select_style,
    style_names,
    time_of_day_phase,
    wind_sentence,
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


# a fixed 12-hour day for deterministic time-of-day checks
_SUNRISE = 1_000_000.0
_SUNSET = 1_043_200.0


def _sun_times(**extra: object) -> dict:
    """A weather sample carrying the fixed daylight window."""
    return {"sunrise_epoch": _SUNRISE, "sunset_epoch": _SUNSET, **extra}


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


def test_default_styles_are_portrait_and_lite_sized() -> None:
    """Every default style targets a mobile-portrait image at 1K."""
    portrait = {"3:4", "4:5", "9:16"}
    assert DEFAULT_STYLES
    for style in DEFAULT_STYLES:
        assert style.aspect_ratio in portrait
        assert style.image_size == "1K"
    assert DEFAULT_STYLES[0].aspect_ratio == DEFAULT_ASPECT_RATIO


@pytest.mark.parametrize("style", DEFAULT_STYLES, ids=style_names())
def test_prompt_includes_all_style_ingredients(style: StyleConfig) -> None:
    """Style, camera, lens and palette are woven in for every style."""
    prompt = build_image_prompt(inverter=_healthy_inverter(), style=style)
    assert style.style in prompt
    assert style.camera in prompt
    assert style.lens in prompt
    assert style.palette is not None and style.palette in prompt


@pytest.mark.parametrize("location", DEFAULT_LOCATIONS, ids=location_names())
def test_prompt_includes_all_location_ingredients(
    location: LocationConfig,
) -> None:
    """Every outdoor location contributes its setting and ambience."""
    prompt = build_image_prompt(inverter=_healthy_inverter(), location=location)
    assert location.setting in prompt
    assert location.lighting in prompt
    assert "Out in the open air beneath the open sky" in prompt


@pytest.mark.parametrize("style", DEFAULT_STYLES, ids=style_names())
def test_prompt_stays_in_budget_for_every_style(style: StyleConfig) -> None:
    """Even the worst-case sample stays inside the 480-token budget."""
    prompt = build_image_prompt(
        inverter=_worst_case_inverter(),
        bms={"active_count": 8, "cell_diff_mv": 250.0},
        weather=_sun_times(
            cloudiness_pct=100,
            midday_pct=0,
            wind_speed_ms=18.0,
            wind_deg=200.0,
            wind_gust_ms=30.0,
        ),
        switches={"load_shed": 1},
        style=style,
        material="weathered corrugated iron",
        now=_SUNRISE + 60 * 60,
    )
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert estimate_prompt_tokens(prompt) <= MAX_PROMPT_TOKENS
    assert len(prompt.split()) <= 400
    assert style.aspect_ratio in prompt


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
    assert "The time of day is" not in prompt
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


def test_find_style_is_case_insensitive() -> None:
    """Style names match regardless of case and padding."""
    style = find_style(DEFAULT_STYLES, "  HYPERREALISTIC ")
    assert style is not None
    assert style.name == "hyperrealistic"


def test_find_style_unknown_returns_none() -> None:
    """An unknown style name resolves to None."""
    assert find_style(DEFAULT_STYLES, "moon_base") is None


def test_select_style_and_location_are_deterministic() -> None:
    """Random selection can be pinned by a seeded generator."""
    assert select_style(DEFAULT_STYLES, random.Random(7)) == select_style(
        DEFAULT_STYLES, random.Random(7)
    )
    assert select_location(DEFAULT_LOCATIONS, random.Random(7)) == (
        select_location(DEFAULT_LOCATIONS, random.Random(7))
    )


def test_select_style_and_location_require_entries() -> None:
    """Empty preset lists are a programming error."""
    with pytest.raises(ValueError):
        select_style(tuple())
    with pytest.raises(ValueError):
        select_location(tuple())


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


@pytest.mark.parametrize(
    ("offset_minutes", "expected"),
    [
        (-90, "evening"),
        (-61, "evening"),
        (-60, "dawn"),
        (0, "dawn"),
        (59, "dawn"),
        (60, "morning"),
        (269, "morning"),
        (270, "midday"),
        (449, "midday"),
        (450, "afternoon"),
        (659, "afternoon"),
        (660, "dusk"),
        (779, "dusk"),
        (780, "evening"),
    ],
)
def test_time_of_day_phase_maps_the_day(offset_minutes: int, expected: str) -> None:
    """Sun-relative windows name every phase of the day."""
    now = _SUNRISE + offset_minutes * 60
    assert time_of_day_phase(_sun_times(), now=now) == expected


def test_time_of_day_phase_needs_usable_sun_times() -> None:
    """Missing or inverted sun times leave the phase unknown."""
    assert time_of_day_phase(None) is None
    assert time_of_day_phase({}) is None
    assert time_of_day_phase({"sunrise_epoch": 100, "sunset_epoch": 100}) is None
    assert time_of_day_phase({"sunrise_epoch": "x", "sunset_epoch": 200}) is None
    assert time_of_day_phase(_sun_times()) in TIME_OF_DAY_PHASES


def test_prompt_states_outdoors_time_of_day_and_wind() -> None:
    """The mandatory weather block is outdoors, named and wind-aware."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        weather=_sun_times(
            cloudiness_pct=20,
            wind_speed_ms=5.4,
            wind_deg=260,
            wind_gust_ms=11.0,
        ),
        now=_SUNRISE + 60 * 60,
    )
    assert "Out in the open air beneath the open sky" in prompt
    assert "The time of day is morning." in prompt
    assert "westerly breeze" in prompt
    assert "Stronger gusts" in prompt


def test_prompt_evening_names_the_night_sky() -> None:
    """The evening phase keeps the stars/moon wording."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(),
        weather=_sun_times(cloudiness_pct=10),
        now=_SUNSET + 120 * 60,
    )
    assert "The time of day is evening." in prompt
    assert "night sky pricked with stars" in prompt


def test_prompt_includes_material() -> None:
    """A requested material is woven into the subject sentence."""
    prompt = build_image_prompt(
        inverter=_healthy_inverter(), material="brushed aluminium"
    )
    assert "made of brushed aluminium" in prompt


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (0.2, "completely still"),
        (2.0, "light breeze"),
        (5.0, "steady breeze"),
        (10.0, "fresh wind"),
        (20.0, "Gale-force winds"),
    ],
)
def test_wind_sentence_bands(speed: float, expected: str) -> None:
    """Wind speed bands map to distinct narrative phrases."""
    sentence = wind_sentence({"wind_speed_ms": speed})
    assert sentence is not None
    assert expected in sentence


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [
        (0, "northerly"),
        (45, "north-easterly"),
        (90, "easterly"),
        (135, "south-easterly"),
        (189, "southerly"),
        (260, "westerly"),
        (315, "north-westerly"),
        (360, "northerly"),
    ],
)
def test_wind_sentence_directions(degrees: float, expected: str) -> None:
    """Bearings become compass adjectives for the wind's origin."""
    sentence = wind_sentence({"wind_speed_ms": 5.0, "wind_deg": degrees})
    assert sentence is not None
    assert expected in sentence


def test_wind_sentence_omits_small_gusts() -> None:
    """A gust close to the mean speed is not dramatised."""
    sentence = wind_sentence({"wind_speed_ms": 5.0, "wind_gust_ms": 6.0})
    assert sentence is not None
    assert "gusts" not in sentence


def test_wind_sentence_requires_a_speed() -> None:
    """Samples without wind data produce no wind clause."""
    assert wind_sentence(None) is None
    assert wind_sentence({}) is None
    assert wind_sentence({"wind_speed_ms": "breezy"}) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("brushed_aluminium", "brushed aluminium"),
        ("  Brushed   Aluminium ", "Brushed Aluminium"),
        ("7-gauge_steel", "7-gauge steel"),
        ("copper!", None),
        ("x" * (MAX_MATERIAL_CHARS + 1), None),
        ("x" * MAX_MATERIAL_CHARS, "x" * MAX_MATERIAL_CHARS),
    ],
)
def test_sanitize_material(text: str | None, expected: str | None) -> None:
    """Only short, plain material phrases survive sanitisation."""
    assert sanitize_material(text) == expected


def test_resolve_imagine_args_defaults_to_random_style_and_location() -> None:
    """No arguments pick a random style and location, and no material."""
    resolved = resolve_imagine_args(None, rng=random.Random(3))
    assert resolved.style in DEFAULT_STYLES
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material is None
    assert resolved.error is None


def test_resolve_imagine_args_single_token_naming_a_style() -> None:
    """A lone style name overrides the random style."""
    resolved = resolve_imagine_args([" HYPERREALISTIC "])
    assert resolved.style is not None
    assert resolved.style.name == "hyperrealistic"
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material is None
    assert resolved.error is None


def test_resolve_imagine_args_single_token_material() -> None:
    """Any other single token is the material for a random style."""
    resolved = resolve_imagine_args(["brushed_aluminium"], rng=random.Random(3))
    assert resolved.style in DEFAULT_STYLES
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material == "brushed aluminium"


def test_resolve_imagine_args_material_then_style() -> None:
    """The second token overrides the random style."""
    resolved = resolve_imagine_args(["copper", "claymation"])
    assert resolved.style is not None
    assert resolved.style.name == "claymation"
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material == "copper"
    assert resolved.error is None


def test_resolve_imagine_args_rejects_a_location_token() -> None:
    """The second parameter is a style; a location name is not accepted."""
    resolved = resolve_imagine_args(["sunny_rooftop"])
    assert resolved.style is None
    assert resolved.location is None
    assert resolved.error == "unknown_style"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["copper", "moon_base"], "unknown_style"),
        (["copper!", "cartoon"], "invalid_material"),
        (["copper", "cartoon", "extra"], "too_many_args"),
        (["~~"], "invalid_material"),
    ],
)
def test_resolve_imagine_args_errors(args: list[str], error: str) -> None:
    """Unusable arguments come back as codes for the bot to phrase."""
    resolved = resolve_imagine_args(args)
    assert resolved.style is None
    assert resolved.error == error
