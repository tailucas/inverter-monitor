#!/usr/bin/env python
"""Pure prompt construction for the Telegram /imagine command.

Prompts follow Google's image-model guidance: the scene is described in
narrative sentences that lead with the operation and fill the guide's
formula -- [subject] + [action] + [location/context] + [composition] +
[style] -- rather than listing tags, and anything that should be absent is
phrased as a positive description instead of a negative instruction.

Telemetry from the inverter, BMS, weather and switch threads is distilled
into the mandatory visual elements: an inverter shown outdoors with the sky
visible, an action and a face that reflect its health, the time of day and
the wind taken from the weather sample, and an explicit load-shedding
indicator. Styles, outdoor locations and subject materials are each picked
at random from their rotations when the user does not choose one, and the
composed prompt stays inside a self-imposed prompt budget that keeps the
request cheap and on-target (the image models' own context windows are far
larger).

No I/O and no framework dependencies: the application threads gather the
telemetry and hand it to these pure helpers, which are unit-tested in
``tests/test_image_prompts.py``.
"""

import random
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# self-imposed prompt budget: the image models' context windows are far
# larger, but short specific prompts stay cheap, fast and on-target
MAX_PROMPT_TOKENS = 700
MAX_PROMPT_CHARS = 2800
# portrait framing fits a mobile chat bubble
DEFAULT_ASPECT_RATIO = "4:5"
DEFAULT_IMAGE_SIZE = "1K"
# load thresholds mirror the alerting defaults in app/__main__.py
DEFAULT_LOAD_WARNING_W = 7000.0
DEFAULT_LOAD_SHED_W = 7000.0
# battery reserves that grade the inverter's mood
BATTERY_UNWELL_PCT = 40.0
BATTERY_STRAINED_PCT = 55.0
BATTERY_HAPPY_PCT = 75.0
# heavy battery draw
BATTERY_MAJOR_DRAW_W = 500.0
# cell voltage spread that shows a balancing problem
CELL_MONITOR_MV = 30.0
CELL_ACTION_MV = 80.0
# the six phases that name the time of day; evening covers the dark hours
TIME_OF_DAY_PHASES = (
    "dawn",
    "morning",
    "midday",
    "afternoon",
    "dusk",
    "evening",
)
# twilight windows straddle sunrise/sunset, midday straddles solar noon
_TWILIGHT_FRACTION = 1 / 12
_MIDDAY_FRACTION = 1 / 8
# Beaufort-inspired wind bands (m/s) for the narrative wind sentence
WIND_STILL_MS = 0.5
WIND_LIGHT_MS = 3.4
WIND_STEADY_MS = 8.0
WIND_FRESH_MS = 13.9
# a gust this much stronger than the mean speed is worth describing
WIND_GUST_GAP_MS = 3.0
# user-supplied materials stay short so they cannot blow the prompt budget
MAX_MATERIAL_CHARS = 40


def _subject_sentence(material: str | None, action: str) -> str:
    """Describe the inverter, its material, its action and the framing.

    The guide asks a prompt to open with a strong verb that names the
    primary operation, so the sentence starts with ``Create`` and carries
    the action clause last.
    """
    subject = "a friendly modern hybrid solar inverter"
    if material:
        subject += f" made of {material}"
    return (
        f"Create a picture of {subject} standing out in the open air "
        "beneath the open sky, centred in the middle of the frame on a low "
        "concrete plinth, with thick power cables looping down to the floor "
        f"and a small glowing display on its front, {action}."
    )


def numeric_value(value: Any) -> float | None:
    """Coerce a telemetry value to float, ignoring booleans and junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except TypeError, ValueError:
        return None


def _truthy(value: Any) -> bool:
    """Interpret a telemetry flag that may arrive as bool, number or text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


# -- locations and styles ------------------------------------------------------


@dataclass(frozen=True)
class LocationConfig:
    """An outdoor location preset for the composed prompt.

    Locations are never chosen by the user: every entry describes an outdoor
    place where the sky is visible, and one is picked at random per image.
    ``setting`` describes the place and ``lighting`` its phase-neutral
    ambience (the time of day comes from the weather sample instead).
    """

    name: str
    setting: str
    lighting: str


DEFAULT_LOCATIONS: tuple[LocationConfig, ...] = (
    LocationConfig(
        name="cosy_home_garage",
        setting=(
            "It stands on the paved apron of a cosy suburban home beside an "
            "open garage door, with a wooden workbench and a pegboard of "
            "hand tools visible inside."
        ),
        lighting="A gentle warm glow spills out from the open garage doorway.",
    ),
    LocationConfig(
        name="sunny_rooftop",
        setting=(
            "It stands on a sunny rooftop terrace with potted succulents, a "
            "water tank and the city skyline far below."
        ),
        lighting="Soft shadows stretch across the rooftop terrace.",
    ),
    LocationConfig(
        name="retro_control_room",
        setting=(
            "It stands on the flat roof of a vintage control building ringed "
            "with dials, gauges and a softly humming console."
        ),
        lighting="A moody teal console glow mixes with one warm service lamp.",
    ),
    LocationConfig(
        name="farmyard_shed",
        setting=(
            "It stands in an open farmyard beside a corrugated-iron shed, "
            "with hay bales, a wheelbarrow and dust motes in the air."
        ),
        lighting="Soft even light rims the props without harsh shadows.",
    ),
    LocationConfig(
        name="orbital_station",
        setting=(
            "It stands on the open observation deck of a small orbital "
            "station behind a railing, with Earth hanging in the black sky "
            "below."
        ),
        lighting="Cool blue starlight from the black sky meets a warm console strip.",
    ),
)


@dataclass(frozen=True)
class StyleConfig:
    """A rendering style preset: the /imagine style parameter's domain.

    ``style`` is the narrative medium sentence; camera, lens, palette and
    the designed ``lighting`` complete the look, ``text`` carries any quoted
    typography the guide's text-rendering rules ask for, and
    ``aspect_ratio``/``image_size`` are forwarded to the API as the image
    response format. Locations always stay outdoors.
    """

    name: str
    style: str
    camera: str
    lens: str
    palette: str | None = None
    lighting: str | None = None
    text: str | None = None
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    image_size: str = DEFAULT_IMAGE_SIZE


DEFAULT_STYLES: tuple[StyleConfig, ...] = (
    StyleConfig(
        name="hyperrealistic",
        style=(
            "The picture is a hyperrealistic photograph with true-to-life "
            "materials, crisp focus and natural colour."
        ),
        camera=(
            "The camera frames it as an eye-level documentary shot with the "
            "inverter dead centre."
        ),
        lens=(
            "A 50 mm lens keeps the subject tack sharp against a softly "
            "blurred background."
        ),
        palette="The palette stays natural and true to every material.",
        lighting="Soft directional daylight keeps every surface believable.",
    ),
    StyleConfig(
        name="cartoon",
        style=(
            "The picture is rendered as a whimsical 3D cartoon with soft "
            "rounded shapes and clean plain surfaces."
        ),
        camera=(
            "The camera frames it as a medium shot from slightly above with "
            "the inverter dead centre."
        ),
        lens="A 35 mm look with gentle depth of field keeps the background soft.",
        palette="The palette leans on warm amber highlights over teal shadows.",
        lighting="Bright bounced light keeps the shadows soft and simple.",
    ),
    StyleConfig(
        name="flat_vector",
        style=(
            "The picture is drawn as a crisp flat vector illustration with "
            "bold outlines and smooth flat colour."
        ),
        camera=(
            "The camera frames it as a slightly low eye-level shot with the "
            "inverter dead centre."
        ),
        lens="Wide 28 mm perspective layers the simple shapes clearly.",
        palette="The palette mixes golden yellow with dusty blue.",
        lighting="Flat even light keeps the shapes crisp and shadow-free.",
    ),
    StyleConfig(
        name="claymation",
        style=(
            "The picture is built like a claymation diorama with fingerprint "
            "textures and chunky props."
        ),
        camera=(
            "The camera frames it as an eye-level medium shot with the "
            "inverter dead centre."
        ),
        lens="A macro-style shallow depth of field melts the background.",
        palette="The palette pairs teal with burnt orange.",
        lighting="A warm tabletop key light makes the clay textures pop.",
    ),
    StyleConfig(
        name="watercolour",
        style=(
            "The picture is painted as a soft watercolour storybook "
            "illustration on textured paper."
        ),
        camera=(
            "The camera frames it as a three-quarter view from waist height "
            "with the inverter dead centre."
        ),
        lens="Loose washes and soft edges keep the drawing airy.",
        palette="The palette stays in faded greens and straw yellows.",
        lighting="Soft diffused daylight keeps the washes pale and airy.",
    ),
    StyleConfig(
        name="stylised_3d",
        style=(
            "The picture is rendered as a polished stylised 3D illustration "
            "with smooth surfaces."
        ),
        camera=("The camera frames it as a centred medium shot from slightly below."),
        lens="A wide-angle feel with crisp detail keeps every panel readable.",
        palette="The palette contrasts deep blue with warm white.",
        lighting=(
            "A crisp studio key light and a cool rim light separate it from "
            "the background."
        ),
    ),
    StyleConfig(
        name="analog_film",
        style=(
            "The picture is shot as if on 1980s colour film with warm analog "
            "colour and pronounced grain."
        ),
        camera=(
            "The camera frames it as a waist-level medium-format portrait "
            "shot with the inverter dead centre."
        ),
        lens="An 80 mm lens at f/2.8 keeps it sharp with creamy falloff.",
        palette="The palette stays warm, faded and gently low in contrast.",
        lighting="Golden hour light rakes across the scene with soft haze.",
        aspect_ratio="4:5",
    ),
    StyleConfig(
        name="action_cam",
        style=(
            "The picture is an immersive action-camera shot with a distorted "
            "ultra-wide view."
        ),
        camera=(
            "The camera is mounted low and close, tilting up at the inverter "
            "dead centre."
        ),
        lens="A fisheye wide-angle lens bends the frame at the edges.",
        palette="The palette is vivid and high contrast.",
        lighting="Crisp daylight gives punchy contrast and a corner flare.",
        aspect_ratio="9:16",
    ),
    StyleConfig(
        name="flash_snapshot",
        style=(
            "The picture is a raw nostalgic snapshot taken on a cheap "
            "disposable camera."
        ),
        camera=(
            "The camera is held at arm's length with the inverter dead "
            "centre and slightly off level."
        ),
        lens="A fixed 35 mm plastic lens keeps the whole scene in focus.",
        palette="The palette carries the dated colour cast of cheap film.",
        lighting="A direct on-camera flash throws a hard shadow on the wall.",
        aspect_ratio="3:2",
    ),
    StyleConfig(
        name="studio_product",
        style=(
            "The picture is a glossy studio product hero shot on a seamless backdrop."
        ),
        camera=("The camera frames it as a clean centred three-quarter product view."),
        lens="A 100 mm macro lens renders every panel and port crisply.",
        palette="The palette stays neutral so every material reads true.",
        lighting=(
            "A three-point softbox setup lights it evenly with gentle reflections."
        ),
        aspect_ratio="1:1",
        image_size="2K",
    ),
    StyleConfig(
        name="cinematic_noir",
        style=(
            "The picture is a moody cinematic still in chiaroscuro light "
            "with deep shadows."
        ),
        camera=(
            "The camera frames it as a low-angle anamorphic wide shot with "
            "the inverter dead centre."
        ),
        lens="A 40 mm anamorphic lens stretches highlights into soft streaks.",
        palette="The palette is graded in muted teal and amber.",
        lighting=(
            "Harsh high-contrast chiaroscuro light carves it out of the darkness."
        ),
        aspect_ratio="21:9",
    ),
    StyleConfig(
        name="typographic_poster",
        style=(
            "The picture is a bold typographic poster with solid colour and "
            "crisp edges."
        ),
        camera=("The camera frames it flat and head-on with the inverter dead centre."),
        lens="A flat graphic perspective keeps every letterform square.",
        palette="The palette uses two bold spot colours.",
        lighting="Even poster-grade light keeps the colours flat and bold.",
        text=(
            'The word "SOLAR" is spelled in heavy blocky white letters '
            "across the top, with the inverter and the sky showing through "
            "inside the letterforms."
        ),
        aspect_ratio="2:3",
        image_size="2K",
    ),
)


def style_names(styles: Sequence[StyleConfig] = DEFAULT_STYLES) -> list[str]:
    """Return the configured style names in presentation order."""
    return [style.name for style in styles]


def find_style(styles: Sequence[StyleConfig], name: str) -> StyleConfig | None:
    """Find a style by name (case-insensitive), or None when unknown."""
    wanted = name.strip().lower()
    for style in styles:
        if style.name.lower() == wanted:
            return style
    return None


def select_style(
    styles: Sequence[StyleConfig] = DEFAULT_STYLES,
    rng: random.Random | None = None,
) -> StyleConfig:
    """Pick a random style configuration (deterministic with a seeded rng)."""
    if not styles:
        raise ValueError("at least one style configuration is required")
    chooser = rng if rng is not None else random.Random()
    return chooser.choice(list(styles))


def location_names(
    locations: Sequence[LocationConfig] = DEFAULT_LOCATIONS,
) -> list[str]:
    """Return the configured location names in presentation order."""
    return [location.name for location in locations]


def find_location(
    locations: Sequence[LocationConfig],
    name: str,
) -> LocationConfig | None:
    """Find a location by name (case-insensitive), or None when unknown."""
    wanted = name.strip().lower()
    for location in locations:
        if location.name.lower() == wanted:
            return location
    return None


def select_location(
    locations: Sequence[LocationConfig] = DEFAULT_LOCATIONS,
    rng: random.Random | None = None,
) -> LocationConfig:
    """Pick a random outdoor location (deterministic with a seeded rng)."""
    if not locations:
        raise ValueError("at least one location configuration is required")
    chooser = rng if rng is not None else random.Random()
    return chooser.choice(list(locations))


# -- /imagine arguments --------------------------------------------------------

_MATERIAL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 '\-]*")

# default subjects: the guide's materiality rule asks for specific physical
# makeup ("navy blue tweed", not "suit jacket"), so the rotation names the
# material and its finish rather than leaving the surface unsaid
DEFAULT_MATERIALS: tuple[str, ...] = (
    "polished brass and walnut",
    "brushed aluminium with smoked glass",
    "matte ceramic with a cobalt blue glaze",
    "hammered copper with a deep patina",
    "carved oak banded with blackened steel",
    "bakelite with polished chrome trim",
    "frosted glass with a soft inner glow",
    "weathered corten steel",
)


def select_material(
    materials: Sequence[str] = DEFAULT_MATERIALS,
    rng: random.Random | None = None,
) -> str:
    """Pick a random default material (deterministic with a seeded rng)."""
    if not materials:
        raise ValueError("at least one default material is required")
    chooser = rng if rng is not None else random.Random()
    return chooser.choice(list(materials))


def sanitize_material(text: str | None) -> str | None:
    """Normalise a user-supplied material into a short prompt-safe phrase.

    Underscores join words (Telegram splits arguments on whitespace), runs of
    whitespace collapse, and anything longer than ``MAX_MATERIAL_CHARS`` or
    carrying other punctuation is rejected so the prompt budget stays safe.
    """
    if not text:
        return None
    cleaned = " ".join(text.replace("_", " ").split())
    if not cleaned or len(cleaned) > MAX_MATERIAL_CHARS:
        return None
    if _MATERIAL_PATTERN.fullmatch(cleaned) is None:
        return None
    return cleaned


@dataclass(frozen=True)
class ImagineArgs:
    """Resolved /imagine arguments: style, location, material or an error."""

    style: StyleConfig | None = None
    location: LocationConfig | None = None
    material: str | None = None
    error: str | None = None


def resolve_imagine_args(
    args: Sequence[str] | None,
    styles: Sequence[StyleConfig] = DEFAULT_STYLES,
    locations: Sequence[LocationConfig] = DEFAULT_LOCATIONS,
    materials: Sequence[str] = DEFAULT_MATERIALS,
    rng: random.Random | None = None,
) -> ImagineArgs:
    """Resolve the optional /imagine arguments into style and material.

    One token names a style when it matches a configured style, and is
    treated as the material otherwise; with two tokens the first is the
    material and the second must name a style. The location is never chosen
    by the user: one of the outdoor locations is picked at random, and one
    of the default materials is picked at random whenever the user does not
    name a material. Materials join words with underscores. Unusable
    arguments come back as error codes for the bot to phrase.
    """
    tokens = [token for token in (args or []) if token.strip()]
    if not tokens:
        return ImagineArgs(
            style=select_style(styles, rng),
            location=select_location(locations, rng),
            material=select_material(materials, rng),
        )
    if len(tokens) > 2:
        return ImagineArgs(error="too_many_args")
    if len(tokens) == 2:
        style = find_style(styles, tokens[1])
        if style is None:
            return ImagineArgs(error="unknown_style")
        material = sanitize_material(tokens[0])
        if material is None:
            return ImagineArgs(error="invalid_material")
        return ImagineArgs(
            style=style,
            location=select_location(locations, rng),
            material=material,
        )
    style = find_style(styles, tokens[0])
    if style is not None:
        return ImagineArgs(
            style=style,
            location=select_location(locations, rng),
            material=select_material(materials, rng),
        )
    if find_location(locations, tokens[0]) is not None:
        # the location stays outdoors and is never user-selected
        return ImagineArgs(error="unknown_style")
    material = sanitize_material(tokens[0])
    if material is None:
        return ImagineArgs(error="invalid_material")
    return ImagineArgs(
        style=select_style(styles, rng),
        location=select_location(locations, rng),
        material=material,
    )


# -- inverter health -----------------------------------------------------------


@dataclass(frozen=True)
class _Health:
    """Distilled inverter condition used to grade the face and props."""

    online: bool
    alert: bool
    soc_pct: float | None
    charging: bool
    major_draw: bool
    load_w: float | None
    pv_w: float | None
    grid_v: float | None


def _health_from(inverter: Mapping[str, Any] | None) -> _Health:
    data = inverter or {}
    pv1 = numeric_value(data.get("pv1_power_w"))
    pv2 = numeric_value(data.get("pv2_power_w"))
    pv_w = None
    if pv1 is not None or pv2 is not None:
        pv_w = (pv1 or 0.0) + (pv2 or 0.0)
    grid1 = numeric_value(data.get("grid_voltage_l1_v"))
    grid2 = numeric_value(data.get("grid_voltage_l2_v"))
    grid_v = None
    if grid1 is not None or grid2 is not None:
        grid_v = max(grid1 or 0.0, grid2 or 0.0)
    battery_power_w = numeric_value(data.get("battery_power_w"))
    return _Health(
        online=bool(data),
        alert=_truthy(data.get("alert")),
        soc_pct=numeric_value(data.get("battery_soc_pct")),
        charging=battery_power_w is not None and battery_power_w < 0,
        major_draw=(
            battery_power_w is not None and battery_power_w >= BATTERY_MAJOR_DRAW_W
        ),
        load_w=numeric_value(data.get("total_load_power_w")),
        pv_w=pv_w,
        grid_v=grid_v,
    )


# -- weather: sky, time of day and wind ---------------------------------------


def _cloud_description(cloudiness: float | None) -> str | None:
    """Turn a cloud-cover percentage into a narrative cloud description."""
    if cloudiness is None:
        return None
    if cloudiness >= 90:
        return "a heavy flat layer of grey cloud"
    if cloudiness >= 60:
        return "ragged grey clouds with only small gaps of blue"
    if cloudiness >= 30:
        return "scattered white clouds drifting across blue sky"
    return "a deep clear blue sky"


def time_of_day_phase(
    weather: Mapping[str, Any] | None,
    now: float | None = None,
) -> str | None:
    """Name the phase of the day from the sample's sunrise/sunset epochs.

    The sun times published with the weather sample make this timezone-free:
    twilight windows straddle each sun event and the midday window straddles
    solar noon, both scaled to the length of the daylight span. The six
    named phases cover the whole day, so evening spans the dark hours.
    """
    data = weather or {}
    sunrise = numeric_value(data.get("sunrise_epoch"))
    sunset = numeric_value(data.get("sunset_epoch"))
    if sunrise is None or sunset is None or sunset <= sunrise:
        return None
    moment = time.time() if now is None else now
    daylight = sunset - sunrise
    twilight = daylight * _TWILIGHT_FRACTION
    midday_span = daylight * _MIDDAY_FRACTION
    solar_noon = sunrise + daylight / 2
    if moment < sunrise - twilight or moment >= sunset + twilight:
        return "evening"
    if moment < sunrise + twilight:
        return "dawn"
    if moment < solar_noon - midday_span:
        return "morning"
    if moment < solar_noon + midday_span:
        return "midday"
    if moment < sunset - twilight:
        return "afternoon"
    return "dusk"


def _time_sentence(phase: str | None) -> str:
    """State the time of day explicitly so the lighting is anchored."""
    if not phase:
        return ""
    return f"The time of day is {phase}."


# how the light reads for every named phase of the day
_PHASE_LIGHT = {
    "dawn": "soft golden light from a sun rising on the horizon",
    "morning": "bright clear light from a climbing sun",
    "midday": "bright daylight from a high sun",
    "afternoon": "warm light from a slowly descending sun",
    "dusk": "low burnt-orange light from a sun sinking on the horizon",
}


def _sky_sentence(
    weather: Mapping[str, Any] | None,
    phase: str | None,
) -> str:
    """Describe the sky and its light, named by phase when one is known."""
    data = weather or {}
    cloudiness = numeric_value(data.get("cloudiness_pct"))
    midday = numeric_value(data.get("midday_pct"))
    if phase == "evening":
        if cloudiness is not None and cloudiness >= 60:
            return (
                "Above the inverter the upper part of the frame holds a dark "
                "night sky with a slim moon hidden behind thick cloud."
            )
        return (
            "Above the inverter the upper part of the frame holds a dark "
            "night sky pricked with stars around a slim crescent moon."
        )
    if phase is not None:
        cloud = _cloud_description(cloudiness)
        light = _PHASE_LIGHT[phase]
        if cloud is None:
            return (
                "Above the inverter the upper part of the frame holds an "
                f"open sky under {light}."
            )
        return (
            "Above the inverter the upper part of the frame holds "
            f"{cloud} under {light}."
        )
    # no sun times in the sample: infer what the solar output allows
    if cloudiness is None and midday is None:
        return (
            "Above the inverter the upper part of the frame holds a calm "
            "neutral sky that keeps the composition open."
        )
    if midday is not None and midday <= 5:
        if cloudiness is not None and cloudiness >= 60:
            return (
                "Above the inverter the upper part of the frame holds a dark "
                "night sky with a slim moon hidden behind thick cloud."
            )
        return (
            "Above the inverter the upper part of the frame holds a dark "
            "night sky pricked with stars around a slim crescent moon."
        )
    cloud = _cloud_description(cloudiness) or "an open sky"
    if midday is not None and midday <= 25:
        light = "low golden light from a sun near the horizon"
    else:
        light = "bright daylight from a high sun"
    return (
        f"Above the inverter the upper part of the frame holds {cloud} under {light}."
    )


# compass adjectives for the direction the wind blows from
_COMPASS_ADJECTIVES = (
    "northerly",
    "north-easterly",
    "easterly",
    "south-easterly",
    "southerly",
    "south-westerly",
    "westerly",
    "north-westerly",
)


def _compass_adjective(degrees: float | None) -> str | None:
    """Convert a meteorological wind bearing into a compass adjective."""
    if degrees is None:
        return None
    index = int(((degrees % 360) + 22.5) // 45) % 8
    return _COMPASS_ADJECTIVES[index]


def wind_sentence(weather: Mapping[str, Any] | None) -> str | None:
    """Describe the wind, or None when the sample carries no wind speed."""
    data = weather or {}
    speed = numeric_value(data.get("wind_speed_ms"))
    if speed is None:
        return None
    direction = _compass_adjective(numeric_value(data.get("wind_deg")))
    if speed < WIND_STILL_MS:
        sentence = "The air around it is completely still."
    elif speed < WIND_LIGHT_MS:
        sentence = "A light breeze drifts through the scene."
        if direction:
            sentence = f"A light {direction} breeze drifts through the scene."
    elif speed < WIND_STEADY_MS:
        sentence = "A steady breeze stirs the scene around it."
        if direction:
            sentence = f"A steady {direction} breeze stirs the scene around it."
    elif speed < WIND_FRESH_MS:
        sentence = "A fresh wind sweeps through the scene."
        if direction:
            sentence = f"A fresh {direction} wind sweeps through the scene."
    else:
        sentence = "Gale-force winds bend everything around it."
        if direction:
            sentence = f"Gale-force {direction} winds bend everything around it."
    gust = numeric_value(data.get("wind_gust_ms"))
    if gust is not None and gust >= speed + WIND_GUST_GAP_MS:
        sentence += " Stronger gusts tug at everything around it."
    return sentence


def _action_sentence(health: _Health) -> str:
    """Name what the inverter is doing for the guide's action slot."""
    if not health.online:
        return "waiting quietly with its fans still"
    if health.alert or (
        health.soc_pct is not None and health.soc_pct < BATTERY_UNWELL_PCT
    ):
        return "straining to keep the lights on"
    if health.charging:
        return "working busily to charge the battery packs"
    if health.major_draw:
        return "working hard to feed the household demand"
    return "humming along as it passes power through the house"


def _face_sentence(health: _Health, load_warning_w: float) -> str:
    """Describe the inverter's face so it reflects its health."""
    if not health.online:
        return (
            "Its display is dark and its face looks sleepy and offline with "
            "closed eyes and an unlit status light."
        )
    if health.alert or (
        health.soc_pct is not None and health.soc_pct < BATTERY_UNWELL_PCT
    ):
        return (
            "Its face shows a worried frown with drooping eyes and a red "
            "status light glowing on its chest."
        )
    if (
        (health.soc_pct is not None and health.soc_pct < BATTERY_STRAINED_PCT)
        or health.major_draw
        or (health.load_w is not None and health.load_w >= load_warning_w)
    ):
        return (
            "Its face shows a strained flat mouth with one small sweat drop "
            "and an amber status light on its chest."
        )
    if health.charging or (
        health.soc_pct is not None and health.soc_pct >= BATTERY_HAPPY_PCT
    ):
        return (
            "Its face beams a big cheerful smile with bright eyes and a "
            "green status light on its chest."
        )
    return (
        "Its face rests in a calm content smile with a steady green status "
        "light on its chest."
    )


def _battery_sentence(health: _Health, bms: Mapping[str, Any] | None) -> str:
    """Describe the battery badge, including any cell imbalance."""
    data = bms or {}
    soc = health.soc_pct
    if soc is None:
        badge = "A battery badge on its chest shows an empty gauge"
    else:
        soc_pct = max(0, min(100, int(round(soc))))
        badge = (
            f"A glowing battery badge on its chest is filled to about {soc_pct} percent"
        )
    diff_mv = numeric_value(data.get("cell_diff_mv"))
    if diff_mv is not None and diff_mv > CELL_ACTION_MV:
        badge += ", with one cell glowing hot orange to show an imbalance."
    elif diff_mv is not None and diff_mv >= CELL_MONITOR_MV:
        badge += ", with its cell stripes glowing slightly unevenly."
    else:
        badge += ", with every cell stripe glowing evenly."
    active = numeric_value(data.get("active_count"))
    if active is not None and active > 1:
        badge += f" Behind it a neat stack of {int(active)} battery packs hums quietly."
    return badge


def _power_sentence(
    health: _Health,
    load_warning_w: float,
    load_shed_w: float,
) -> str:
    """Describe solar, grid and load props around the inverter."""
    sentences: list[str] = []
    if health.pv_w is None:
        pass
    elif health.pv_w >= 1500:
        sentences.append(
            "Golden sunbeams stream down onto two roof solar panels beside "
            "it that glow brightly."
        )
    elif health.pv_w >= 200:
        sentences.append(
            "Thin sunbeams reach two roof solar panels beside it that glow softly."
        )
    else:
        sentences.append("Two roof solar panels beside it rest dim and still.")
    if health.grid_v is None:
        pass
    elif health.grid_v >= 90:
        sentences.append("A small pylon behind it is lit by a steady grid connection.")
    else:
        sentences.append("A small pylon behind it stands dark and disconnected.")
    if health.load_w is None:
        pass
    elif health.load_w >= load_shed_w:
        sentences.append(
            "A power meter on the wall beside it is buried deep in the red zone."
        )
    elif health.load_w >= load_warning_w * 0.8:
        sentences.append(
            "A power meter on the wall beside it leans high into the amber zone."
        )
    else:
        sentences.append("A power meter on the wall beside it sits in the green.")
    return " ".join(sentences)


# -- load shedding -------------------------------------------------------------


@dataclass(frozen=True)
class LoadShedState:
    """Whether load shedding is needed, and the reason to show for it."""

    needed: bool
    reason: str | None = None


def load_shed_state(
    inverter: Mapping[str, Any] | None = None,
    switches: Mapping[str, Any] | None = None,
    load_warning_w: float = DEFAULT_LOAD_WARNING_W,
    load_shed_w: float | None = None,
) -> LoadShedState:
    """Derive the load-shedding indicator from switch and inverter state.

    The switch-bank rationing latches are authoritative; when no switch
    reason is active, the live load is compared against the shed threshold.
    """
    shed_w = DEFAULT_LOAD_SHED_W if load_shed_w is None else load_shed_w
    data = switches or {}
    if _truthy(data.get("load_shed")):
        return LoadShedState(True, "the household load is too high")
    if _truthy(data.get("overcast")):
        return LoadShedState(True, "the sky is completely overcast")
    if _truthy(data.get("battery_ration")):
        return LoadShedState(True, "the battery reserve is running low")
    if _truthy(data.get("surplus_ration")):
        return LoadShedState(True, "there is no solar surplus to spare")
    load_w = numeric_value((inverter or {}).get("total_load_power_w"))
    if load_w is not None and load_w >= shed_w:
        return LoadShedState(True, "the household load is too high")
    return LoadShedState(False, None)


def _shed_sentence(state: LoadShedState) -> str:
    """Describe the load-shedding indicator (present or absent)."""
    if state.needed:
        reason = state.reason or "the demand is too high"
        return (
            "A bright red warning sign with a crossed-out plug stands beside "
            "it, two non-essential appliances sit switched off at the wall, "
            "and a red lamp shows that load shedding is needed because "
            f"{reason}."
        )
    return (
        "Every appliance around it glows happily and a green lamp shows that "
        "load shedding is not needed right now."
    )


# -- prompt composition --------------------------------------------------------


def _composition_sentence(aspect_ratio: str) -> str:
    """State a framing that agrees with the requested aspect ratio."""
    parts = aspect_ratio.split(":")
    orientation = "vertical portrait"
    if len(parts) == 2:
        try:
            width, height = int(parts[0]), int(parts[1])
        except ValueError:
            width = height = 0
        if width and height:
            if width == height:
                orientation = "square"
            elif width > height:
                orientation = "wide landscape"
    return (
        f"The whole picture is a {orientation} composition in a "
        f"{aspect_ratio} aspect ratio."
    )


def _compose(mandatory: Sequence[str | None], optional: Sequence[str | None]) -> str:
    """Join mandatory clauses and add optional ones while they still fit."""
    prompt = " ".join(clause for clause in mandatory if clause)
    for clause in optional:
        if not clause:
            continue
        candidate = f"{prompt} {clause}"
        if len(candidate) <= MAX_PROMPT_CHARS:
            prompt = candidate
    if len(prompt) > MAX_PROMPT_CHARS:
        # last-resort guard: cut back to the nearest sentence boundary
        prompt = prompt[:MAX_PROMPT_CHARS]
        boundary = prompt.rfind(". ")
        if boundary > MAX_PROMPT_CHARS // 2:
            prompt = prompt[: boundary + 1]
    return prompt


def build_image_prompt(
    inverter: Mapping[str, Any] | None = None,
    bms: Mapping[str, Any] | None = None,
    weather: Mapping[str, Any] | None = None,
    switches: Mapping[str, Any] | None = None,
    style: StyleConfig = DEFAULT_STYLES[0],
    location: LocationConfig = DEFAULT_LOCATIONS[0],
    load_warning_w: float = DEFAULT_LOAD_WARNING_W,
    load_shed_w: float | None = None,
    material: str | None = None,
    now: float | None = None,
) -> str:
    """Compose a narrative, budgeted image prompt from live telemetry."""
    shed_w = DEFAULT_LOAD_SHED_W if load_shed_w is None else load_shed_w
    health = _health_from(inverter)
    phase = time_of_day_phase(weather, now)
    mandatory = [
        _subject_sentence(material, _action_sentence(health)),
        _composition_sentence(style.aspect_ratio),
        location.setting,
        _time_sentence(phase),
        _sky_sentence(weather, phase),
        wind_sentence(weather),
        _face_sentence(health, load_warning_w),
        _battery_sentence(health, bms),
        _power_sentence(health, load_warning_w, shed_w),
        _shed_sentence(load_shed_state(inverter, switches, load_warning_w, shed_w)),
        style.style,
        style.text,
        location.lighting,
        style.lighting,
        style.camera,
    ]
    optional = [
        style.lens,
        style.palette,
    ]
    return _compose(mandatory, optional)


def estimate_prompt_tokens(prompt: str) -> int:
    """Estimate prompt tokens from its character count (4 chars/token)."""
    return (len(prompt) + 3) // 4


@dataclass(frozen=True)
class ImaginePrompt:
    """A composed prompt plus the caption, style and location behind it."""

    prompt: str
    caption: str
    style: StyleConfig
    location: LocationConfig
    time_of_day: str | None = None
