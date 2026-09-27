#!/usr/bin/env python
"""Unit tests for the pure image-prompt builder."""

import random

import pytest

from app.image_prompts import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_IMAGE_SIZE,
    DEFAULT_LOCATIONS,
    DEFAULT_MATERIALS,
    DEFAULT_STYLES,
    MAX_ARG_CHARS,
    MAX_PROMPT_CHARS,
    MAX_PROMPT_TOKENS,
    TIME_OF_DAY_PHASES,
    StyleConfig,
    build_image_prompt,
    custom_style,
    estimate_prompt_tokens,
    find_style,
    load_shed_state,
    numeric_value,
    resolve_imagine_args,
    sanitize_slug,
    select_option,
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


def test_prompt_action_reflects_battery_charging() -> None:
    """A charging battery puts the charging action in the subject sentence."""
    prompt = build_image_prompt(inverter=_healthy_inverter())
    assert "working busily to charge the battery packs" in prompt


def test_prompt_action_reflects_heavy_draw() -> None:
    """A heavy discharge shows the inverter working hard to keep up."""
    inverter = _healthy_inverter() | {
        "battery_power_w": 900.0,
        "battery_soc_pct": 70.0,
    }
    prompt = build_image_prompt(inverter=inverter)
    assert "working hard to feed the household demand" in prompt


def test_prompt_action_reflects_offline_inverter() -> None:
    """No live sample leaves the inverter waiting quietly."""
    prompt = build_image_prompt()
    assert "waiting quietly with its fans still" in prompt


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


def test_default_styles_use_supported_ratios_and_sizes() -> None:
    """Every default style targets a supported ratio and image size."""
    supported_ratios = {"1:1", "3:2", "2:3", "3:4", "4:5", "9:16", "16:9", "21:9"}
    supported_sizes = {"512", "1K", "2K", "4K"}
    assert DEFAULT_STYLES
    for style in DEFAULT_STYLES:
        assert style.aspect_ratio in supported_ratios
        assert style.image_size in supported_sizes
    assert DEFAULT_STYLES[0].aspect_ratio == DEFAULT_ASPECT_RATIO
    assert DEFAULT_STYLES[0].image_size == DEFAULT_IMAGE_SIZE


@pytest.mark.parametrize("style", DEFAULT_STYLES, ids=style_names())
def test_prompt_includes_all_style_ingredients(style: StyleConfig) -> None:
    """Style, camera, lens and palette are woven in for every style."""
    prompt = build_image_prompt(inverter=_healthy_inverter(), style=style)
    assert style.style in prompt
    assert style.camera is not None and style.camera in prompt
    assert style.lens is not None and style.lens in prompt
    assert style.palette is not None and style.palette in prompt
    assert style.lighting is not None and style.lighting in prompt
    if style.text is not None:
        assert style.text in prompt


@pytest.mark.parametrize(
    "location",
    DEFAULT_LOCATIONS,
    ids=[f"setting_{index}" for index in range(len(DEFAULT_LOCATIONS))],
)
def test_prompt_includes_all_location_ingredients(location: str) -> None:
    """Every outdoor setting contributes its scenery verbatim."""
    prompt = build_image_prompt(inverter=_healthy_inverter(), location=location)
    assert location in prompt
    assert "out in the open air beneath the open sky" in prompt.lower()


@pytest.mark.parametrize("style", DEFAULT_STYLES, ids=style_names())
def test_prompt_stays_in_budget_for_every_style(style: StyleConfig) -> None:
    """Even the worst-case sample stays inside the self-imposed budget."""
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
    assert len(prompt.split()) <= 500
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


def test_prompt_opens_with_a_strong_verb() -> None:
    """The prompt leads with the operation, per the guide's guidance."""
    prompt = build_image_prompt(inverter=_healthy_inverter())
    assert prompt.startswith("Create a picture of")


@pytest.mark.parametrize(
    ("aspect_ratio", "framing"),
    [
        ("4:5", "vertical portrait"),
        ("1:1", "square"),
        ("21:9", "wide landscape"),
    ],
)
def test_prompt_composition_matches_aspect_ratio(
    aspect_ratio: str, framing: str
) -> None:
    """The composition sentence agrees with the requested aspect ratio."""
    style = StyleConfig(
        name="probe",
        style="The picture is a probe.",
        camera="The camera frames it from the front.",
        lens="A 50 mm lens keeps it sharp.",
        aspect_ratio=aspect_ratio,
    )
    prompt = build_image_prompt(inverter=_healthy_inverter(), style=style)
    assert f"{framing} composition in a {aspect_ratio} aspect ratio" in prompt


def test_typographic_poster_quotes_its_rendered_text() -> None:
    """The poster style names its text in quotes, per the guide's rules."""
    style = find_style(DEFAULT_STYLES, "typographic_poster")
    assert style is not None
    assert style.text is not None
    assert '"SOLAR"' in style.text


def test_find_style_is_case_insensitive() -> None:
    """Style names match regardless of case and padding."""
    style = find_style(DEFAULT_STYLES, "  HYPERREALISTIC ")
    assert style is not None
    assert style.name == "hyperrealistic"


def test_find_style_unknown_returns_none() -> None:
    """An unknown style name resolves to None."""
    assert find_style(DEFAULT_STYLES, "moon_base") is None


def test_select_style_is_deterministic() -> None:
    """Random style selection can be pinned by a seeded generator."""
    assert select_style(DEFAULT_STYLES, random.Random(7)) == select_style(
        DEFAULT_STYLES, random.Random(7)
    )


def test_select_option_is_deterministic() -> None:
    """Seeded draws from a string rotation can be pinned."""
    assert select_option(DEFAULT_LOCATIONS, random.Random(7)) == select_option(
        DEFAULT_LOCATIONS, random.Random(7)
    )
    assert select_option(DEFAULT_MATERIALS, random.Random(7)) == select_option(
        DEFAULT_MATERIALS, random.Random(7)
    )


def test_empty_rotations_are_a_programming_error() -> None:
    """Empty rotations are a programming error."""
    with pytest.raises(ValueError):
        select_style(tuple())
    with pytest.raises(ValueError):
        select_option(tuple())


def test_select_style_avoids_the_previous_choice() -> None:
    """The style rotation never repeats the last style shown."""
    rng = random.Random(5)
    avoided = DEFAULT_STYLES[0].name
    picks = {select_style(DEFAULT_STYLES, rng, avoid=avoided).name for _ in range(200)}
    assert picks == {style.name for style in DEFAULT_STYLES} - {avoided}


def test_select_option_avoids_the_previous_choice() -> None:
    """A string rotation never repeats the last option shown."""
    rng = random.Random(5)
    avoided = DEFAULT_LOCATIONS[0]
    picks = {select_option(DEFAULT_LOCATIONS, rng, avoid=avoided) for _ in range(200)}
    assert picks == set(DEFAULT_LOCATIONS) - {avoided}


def test_single_entry_rotation_returns_it_despite_an_avoid() -> None:
    """A rotation holding only the avoided entry still returns that entry."""
    style = select_style((DEFAULT_STYLES[0],), avoid=DEFAULT_STYLES[0].name)
    assert style == DEFAULT_STYLES[0]
    location = select_option((DEFAULT_LOCATIONS[0],), avoid=DEFAULT_LOCATIONS[0])
    assert location == DEFAULT_LOCATIONS[0]


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
    assert "out in the open air beneath the open sky" in prompt.lower()
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
        ("copper", "copper"),
        ("brushed_aluminium", "brushed_aluminium"),
        ("7_gauge_steel", "7_gauge_steel"),
        ("  copper  ", "copper"),
        ("Brushed Aluminium", None),
        ("COPPER", None),
        ("brushed-aluminium", None),
        ("copper!", None),
        ("double__underscore", None),
        ("_leading", None),
        ("trailing_", None),
        ("x" * (MAX_ARG_CHARS + 1), None),
        ("x" * MAX_ARG_CHARS, "x" * MAX_ARG_CHARS),
    ],
)
def test_sanitize_slug(text: str | None, expected: str | None) -> None:
    """Only short snake_case slugs survive sanitisation."""
    assert sanitize_slug(text) == expected


def test_default_materials_are_prompt_safe() -> None:
    """Every default material is a short lowercase prompt phrase."""
    assert DEFAULT_MATERIALS
    for material in DEFAULT_MATERIALS:
        assert 0 < len(material) <= MAX_ARG_CHARS
        assert material == material.lower()


def test_unknown_avoid_keeps_the_full_rotation() -> None:
    """An avoid naming nothing leaves every entry in the draw."""
    rng = random.Random(2)
    picks = {
        select_option(DEFAULT_MATERIALS, rng, avoid="nothing like this")
        for _ in range(200)
    }
    assert picks == set(DEFAULT_MATERIALS)


def test_prompt_weaves_the_default_material_into_the_subject() -> None:
    """A default-rotation material reaches the subject sentence."""
    resolved = resolve_imagine_args(None, rng=random.Random(11))
    prompt = build_image_prompt(
        inverter=_healthy_inverter(), material=resolved.material
    )
    assert f"made of {resolved.material}" in prompt


def test_resolve_imagine_args_defaults_to_the_full_rotation() -> None:
    """No arguments pick a random style, location and material."""
    resolved = resolve_imagine_args(None, rng=random.Random(3))
    assert resolved.style in DEFAULT_STYLES
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material in DEFAULT_MATERIALS
    assert resolved.error is None


def test_resolve_imagine_args_avoids_the_previous_picks() -> None:
    """No arguments rotate away from every previously shown choice."""
    resolved = resolve_imagine_args(
        None,
        rng=random.Random(3),
        avoid_style=DEFAULT_STYLES[0].name,
        avoid_location=DEFAULT_LOCATIONS[0],
        avoid_material=DEFAULT_MATERIALS[0],
    )
    assert resolved.style is not None
    assert resolved.location is not None
    assert resolved.style.name != DEFAULT_STYLES[0].name
    assert resolved.location != DEFAULT_LOCATIONS[0]
    assert resolved.material != DEFAULT_MATERIALS[0]
    assert resolved.error is None


def test_resolve_imagine_args_rotates_around_a_chosen_material() -> None:
    """A named material wins the draw; style and location still vary."""
    resolved = resolve_imagine_args(
        ["copper"],
        rng=random.Random(3),
        avoid_style=DEFAULT_STYLES[0].name,
        avoid_location=DEFAULT_LOCATIONS[0],
        avoid_material=DEFAULT_MATERIALS[0],
    )
    assert resolved.material == "copper"
    assert resolved.style is not None
    assert resolved.location is not None
    assert resolved.style.name != DEFAULT_STYLES[0].name
    assert resolved.location != DEFAULT_LOCATIONS[0]


def test_resolve_imagine_args_rotates_around_a_chosen_style() -> None:
    """A named style wins the draw; material and location still vary."""
    resolved = resolve_imagine_args(
        ["claymation"],
        rng=random.Random(8),
        avoid_location=DEFAULT_LOCATIONS[1],
        avoid_material=DEFAULT_MATERIALS[1],
    )
    assert resolved.style is not None
    assert resolved.location is not None
    assert resolved.style.name == "claymation"
    assert resolved.location != DEFAULT_LOCATIONS[1]
    assert resolved.material != DEFAULT_MATERIALS[1]


def test_resolve_imagine_args_rotates_location_only_when_both_given() -> None:
    """Named material and style still rotate the location."""
    resolved = resolve_imagine_args(
        ["copper", "cartoon"],
        rng=random.Random(4),
        avoid_location=DEFAULT_LOCATIONS[2],
    )
    assert resolved.material == "copper"
    assert resolved.style is not None
    assert resolved.location is not None
    assert resolved.style.name == "cartoon"
    assert resolved.location != DEFAULT_LOCATIONS[2]


def test_unseeded_rotations_vary_between_calls() -> None:
    """Unseeded calls draw fresh entropy, so the rotation actually rotates."""
    triples: set[tuple[str, str, str | None]] = set()
    for _ in range(30):
        resolved = resolve_imagine_args(None)
        style = resolved.style
        location = resolved.location
        assert style is not None
        assert location is not None
        triples.add((style.name, location, resolved.material))
    assert len(triples) > 1


def test_resolve_imagine_args_single_token_naming_a_style() -> None:
    """A lone style name overrides the random style."""
    resolved = resolve_imagine_args(["hyperrealistic"])
    assert resolved.style is not None
    assert resolved.style.name == "hyperrealistic"
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material in DEFAULT_MATERIALS
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


def test_resolve_imagine_args_custom_style() -> None:
    """An unknown snake_case style is passed through as a custom style."""
    resolved = resolve_imagine_args(["copper", "cyberpunk_neon"])
    assert resolved.style is not None
    assert resolved.style.name == "cyberpunk_neon"
    assert "cyberpunk neon" in resolved.style.style
    assert resolved.style.camera is None
    assert resolved.style.lens is None
    assert resolved.style.aspect_ratio == DEFAULT_ASPECT_RATIO
    assert resolved.style.image_size == DEFAULT_IMAGE_SIZE
    assert resolved.material == "copper"
    assert resolved.error is None


def test_resolve_imagine_args_builtin_style_keeps_its_format() -> None:
    """A built-in style still carries its preset response format."""
    resolved = resolve_imagine_args(["copper", "cinematic_noir"])
    assert resolved.style is not None
    assert resolved.style.name == "cinematic_noir"
    assert resolved.style.aspect_ratio == "21:9"
    assert resolved.material == "copper"
    assert resolved.error is None


def test_custom_style_contributes_only_its_sentence() -> None:
    """A custom style reaches the prompt without preset framing."""
    style = custom_style("cyberpunk_neon")
    prompt = build_image_prompt(inverter=_healthy_inverter(), style=style)
    assert "rendered in a cyberpunk neon style" in prompt
    assert style.aspect_ratio == DEFAULT_ASPECT_RATIO
    assert style.image_size == DEFAULT_IMAGE_SIZE


def test_resolve_imagine_args_treats_a_location_token_as_a_material() -> None:
    """The location is never user-selectable: a location-like token is material."""
    resolved = resolve_imagine_args(["sunny_rooftop"])
    assert resolved.style in DEFAULT_STYLES
    assert resolved.location in DEFAULT_LOCATIONS
    assert resolved.material == "sunny rooftop"
    assert resolved.error is None


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["copper", "Style_With_Spaces!"], "invalid_style"),
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
