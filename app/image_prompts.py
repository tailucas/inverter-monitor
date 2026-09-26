#!/usr/bin/env python
"""Pure prompt construction for the Telegram /imagine command.

Prompts follow Google's image-model guidance: the scene is described in
narrative sentences (subject, setting, style, lighting, camera, lens) rather
than a list of tags, and anything that should be absent is phrased as a
positive description instead of a negative instruction.

Telemetry from the inverter, BMS, weather and switch threads is distilled
into the mandatory visual elements: the inverter centred in the frame, the
weather above it, a face that reflects its health, and an explicit
load-shedding indicator. The composed prompt stays inside the model's
480-token prompt budget (roughly 2000 characters).

No I/O and no framework dependencies: the application threads gather the
telemetry and hand it to these pure helpers, which are unit-tested in
``tests/test_image_prompts.py``.
"""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# image-model prompt budget: 480 tokens, roughly 2000 characters
MAX_PROMPT_TOKENS = 480
MAX_PROMPT_CHARS = 2000
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

_SUBJECT = (
    "Centred in the middle of the frame stands a friendly modern hybrid "
    "solar inverter on a low concrete plinth, with thick power cables "
    "looping down to the floor and a small glowing display on its front"
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


# -- scene configurations ------------------------------------------------------


@dataclass(frozen=True)
class SceneConfig:
    """A narrative scene preset woven into the composed prompt.

    Each field is a complete sentence (or clause pair) describing one of the
    image-model prompt ingredients; ``aspect_ratio`` and ``image_size`` are
    forwarded to the API as the image response format.
    """

    name: str
    setting: str
    style: str
    lighting: str
    camera: str
    lens: str
    palette: str | None = None
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    image_size: str = DEFAULT_IMAGE_SIZE


DEFAULT_SCENES: tuple[SceneConfig, ...] = (
    SceneConfig(
        name="cosy_home_garage",
        setting=(
            "It stands in a tidy home garage with a wooden workbench, a "
            "pegboard of hand tools and coiled cables on the wall behind it."
        ),
        style=(
            "The picture is rendered as a whimsical 3D cartoon with soft "
            "rounded shapes and clean plain surfaces."
        ),
        lighting="Warm afternoon sunlight falls through a high side window.",
        camera=(
            "The camera frames it as a medium shot from slightly above with "
            "the inverter dead centre."
        ),
        lens="A 35 mm look with gentle depth of field keeps the background soft.",
        palette="The palette leans on warm amber highlights over teal shadows.",
    ),
    SceneConfig(
        name="sunny_rooftop",
        setting=(
            "It stands on a sunny rooftop terrace with potted succulents, a "
            "water tank and the city skyline far below."
        ),
        style=(
            "The picture is drawn as a crisp flat vector illustration with "
            "bold outlines and smooth flat colour."
        ),
        lighting="Low golden-hour sun backlights the scene with long soft shadows.",
        camera=(
            "The camera frames it as a slightly low eye-level shot with the "
            "inverter dead centre."
        ),
        lens="Wide 28 mm perspective layers the simple shapes clearly.",
        palette="The palette mixes golden yellow with dusty blue.",
    ),
    SceneConfig(
        name="retro_control_room",
        setting=(
            "It stands in a vintage control room lined with dials, gauges and "
            "a softly humming console."
        ),
        style=(
            "The picture is built like a claymation diorama with fingerprint "
            "textures and chunky props."
        ),
        lighting="A moody teal console glow mixes with one warm desk lamp.",
        camera=(
            "The camera frames it as an eye-level medium shot with the "
            "inverter dead centre."
        ),
        lens="A macro-style shallow depth of field melts the background.",
        palette="The palette pairs teal with burnt orange.",
    ),
    SceneConfig(
        name="farmyard_shed",
        setting=(
            "It stands inside an open corrugated-iron farm shed with hay "
            "bales, a wheelbarrow and dust motes in the air."
        ),
        style=(
            "The picture is painted as a soft watercolour storybook "
            "illustration on textured paper."
        ),
        lighting="Gentle overcast daylight washes the scene with no harsh shadows.",
        camera=(
            "The camera frames it as a three-quarter view from waist height "
            "with the inverter dead centre."
        ),
        lens="Loose washes and soft edges keep the drawing airy.",
        palette="The palette stays in faded greens and straw yellows.",
        aspect_ratio="3:4",
    ),
    SceneConfig(
        name="orbital_station",
        setting=(
            "It stands on the observation deck of a small orbital station "
            "with a round porthole showing Earth below."
        ),
        style=(
            "The picture is rendered as a polished stylised 3D illustration "
            "with smooth surfaces."
        ),
        lighting="Cool blue starlight from the porthole meets a warm console strip.",
        camera=("The camera frames it as a centred medium shot from slightly below."),
        lens="A wide-angle feel with crisp detail keeps every panel readable.",
        palette="The palette contrasts deep blue with warm white.",
        aspect_ratio="9:16",
    ),
)


def scene_names(scenes: Sequence[SceneConfig] = DEFAULT_SCENES) -> list[str]:
    """Return the configured scene names in presentation order."""
    return [scene.name for scene in scenes]


def find_scene(scenes: Sequence[SceneConfig], name: str) -> SceneConfig | None:
    """Find a scene by name (case-insensitive), or None when unknown."""
    wanted = name.strip().lower()
    for scene in scenes:
        if scene.name.lower() == wanted:
            return scene
    return None


def select_scene(
    scenes: Sequence[SceneConfig] = DEFAULT_SCENES,
    rng: random.Random | None = None,
) -> SceneConfig:
    """Pick a random scene configuration (deterministic with a seeded rng)."""
    if not scenes:
        raise ValueError("at least one scene configuration is required")
    chooser = rng if rng is not None else random.Random()
    return chooser.choice(list(scenes))


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


def _sky_sentence(weather: Mapping[str, Any] | None) -> str:
    """Describe the weather in the upper part of the frame."""
    data = weather or {}
    cloudiness = numeric_value(data.get("cloudiness_pct"))
    midday = numeric_value(data.get("midday_pct"))
    if cloudiness is None and midday is None:
        return (
            "Above the inverter the upper part of the frame holds a calm "
            "neutral sky that keeps the composition open."
        )
    if cloudiness is not None and cloudiness >= 90:
        cloud = "a heavy flat layer of grey cloud"
    elif cloudiness is not None and cloudiness >= 60:
        cloud = "ragged grey clouds with only small gaps of blue"
    elif cloudiness is not None and cloudiness >= 30:
        cloud = "scattered white clouds drifting across blue sky"
    else:
        cloud = "a deep clear blue sky"
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
    if midday is not None and midday <= 25:
        light = "low golden light from a sun near the horizon"
    else:
        light = "bright daylight from a high sun"
    return (
        f"Above the inverter the upper part of the frame holds {cloud} under {light}."
    )


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


def _compose(mandatory: Sequence[str], optional: Sequence[str | None]) -> str:
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
    scene: SceneConfig = DEFAULT_SCENES[0],
    load_warning_w: float = DEFAULT_LOAD_WARNING_W,
    load_shed_w: float | None = None,
) -> str:
    """Compose a narrative, budgeted image prompt from live telemetry."""
    shed_w = DEFAULT_LOAD_SHED_W if load_shed_w is None else load_shed_w
    health = _health_from(inverter)
    mandatory = [
        f"{_SUBJECT}. {scene.setting}",
        _sky_sentence(weather),
        _face_sentence(health, load_warning_w),
        _battery_sentence(health, bms),
        _power_sentence(health, load_warning_w, shed_w),
        _shed_sentence(load_shed_state(inverter, switches, load_warning_w, shed_w)),
        scene.style,
        scene.lighting,
        scene.camera,
    ]
    optional = [
        scene.lens,
        scene.palette,
        (
            "The whole picture is a vertical portrait composition in a "
            f"{scene.aspect_ratio} aspect ratio."
        ),
    ]
    return _compose(mandatory, optional)


def estimate_prompt_tokens(prompt: str) -> int:
    """Estimate prompt tokens from its character count (4 chars/token)."""
    return (len(prompt) + 3) // 4


@dataclass(frozen=True)
class ImaginePrompt:
    """A composed prompt plus the photo caption and scene it came from."""

    prompt: str
    caption: str
    scene: SceneConfig
