#!/usr/bin/env python
"""Pure helper functions and data structures for the Telegram bot.

Status buffer, Markdown status formatter, matplotlib chart renderers,
and plain-text caption builder. No Telegram library imports, no async
handlers, no AppThread -- those live in `app.bot`.
"""

import html
import io
from collections.abc import Callable
from threading import Lock
from typing import Any

import emoji
import matplotlib
import pandas as pd
from tailucas_pylib import APP_NAME, DEVICE_NAME_BASE, app_config

from app.image_prompts import (
    DEFAULT_LOAD_WARNING_W,
    load_shed_state,
    numeric_value,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt

URL_WORKER_TELEGRAM = "inproc://telegram"

DEFAULT_HISTORY_HOURS = app_config.getint("telegram", "history_hours", fallback=24)


def _get_telegram_token(creds_obj: Any) -> str:
    """Retrieve the Telegram bot API token from the credential store."""
    token: str = creds_obj.get_creds(f"Telegram/{APP_NAME}/token")
    return token


# -- BMS summary buffer (thread-safe) -----------------------------------------


class BmsSummaryBuffer:
    """Thread-safe buffer holding only the latest BMS summary.

    The inverter data is queried live on demand; only the derived BMS summary
    (which comes via the ZMQ fan-out from EventProcessor) is cached.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {}

    def update(self, data: dict[str, Any]) -> None:
        """Store the latest BMS summary (copy semantics)."""
        with self._lock:
            self._data = data.copy()

    def summary(self) -> dict[str, Any]:
        """Return a copy of the current BMS summary."""
        with self._lock:
            return self._data.copy()


# -- weather buffer (thread-safe) ----------------------------------------------


class WeatherBuffer:
    """Thread-safe buffer holding the latest weather sample."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {}

    def update(self, data: dict[str, Any]) -> None:
        """Store the latest weather sample (copy semantics)."""
        with self._lock:
            self._data = data.copy()

    def summary(self) -> dict[str, Any]:
        """Return a copy of the current weather sample."""
        with self._lock:
            return self._data.copy()


# -- switch stats buffer (thread-safe) -----------------------------------------


class SwitchStatsBuffer:
    """Thread-safe buffer holding the latest switch/rationing stats."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {}

    def update(self, data: dict[str, Any]) -> None:
        """Store the latest switch stats (copy semantics)."""
        with self._lock:
            self._data = data.copy()

    def summary(self) -> dict[str, Any]:
        """Return a copy of the current switch stats."""
        with self._lock:
            return self._data.copy()


# -- BMS summary derivation helper --------------------------------------------


def build_bms_summary(battery_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive a compact BMS summary dict from a battery payload list.

    Each entry is expected to have a ``metrics`` dict with keys like
    ``voltage_v``, ``min_cell_v``, ``max_cell_v``, ``cell_diff_mv``.
    The first entry that provides a value wins for each key.
    """
    summary: dict[str, Any] = {"active_count": len(battery_items)}
    _PICK_KEYS = ("voltage_v", "min_cell_v", "max_cell_v", "cell_diff_mv")
    for entry in battery_items:
        metrics = entry.get("metrics", {})
        if not isinstance(metrics, dict):
            continue
        for k in _PICK_KEYS:
            v = metrics.get(k)
            if v is not None:
                summary.setdefault(k, v)
    return summary


# -- status formatter (Markdown output) ----------------------------------------


def _format_cloudiness(weather: dict[str, Any]) -> str:
    """Render the latest cloudiness percentage for the status message."""
    value = weather.get("cloudiness_pct")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "\u2014"
    return f"{float(value):.0f}%"


def format_status_message(
    inverter: dict[str, Any],
    bms_summary: dict[str, Any],
    weather: dict[str, Any] | None = None,
) -> str:
    """Build a compact Markdown status message from live telemetry.

    Args:
        inverter: A dict of scalar inverter telemetry fields.
        bms_summary: A dict of BMS summary fields (e.g. ``active_count``,
            ``voltage_v``, ``min_cell_v``, ``max_cell_v``, ``cell_diff_mv``).
        weather: The latest weather sample (e.g. ``cloudiness_pct``).

    Returns:
        A Markdown-formatted status string.
    """
    bms = bms_summary

    def _get(key: str, unit: str = "", default: str = "\u2014") -> str:
        val = inverter.get(key)
        if val is None:
            return default
        try:
            v = float(val)
            return f"{v:.1f} {unit}".strip() if unit else f"{v}"
        except ValueError, TypeError:
            return str(val)

    lines: list[str] = [
        "*Inverter Status*",
        f"SOC: `{_get('battery_soc_pct', '%')}`   "
        f"Batt: `{_get('battery_voltage_v', 'V')}` / "
        f"`{_get('battery_power_w', 'W')}`",
        f"PV1: `{_get('pv1_power_w', 'W')}`   "
        f"PV2: `{_get('pv2_power_w', 'W')}`   "
        f"Load: `{_get('total_load_power_w', 'W')}`",
        f"Grid: `{_get('grid_voltage_l1_v', 'V')}` / "
        f"`{_get('grid_voltage_l2_v', 'V')}`",
        f"Daily PV: `{_get('daily_production_kwh', 'kWh')}`  "
        f"Load: `{_get('daily_load_consumption_kwh', 'kWh')}`",
        f"Work mode: `{inverter.get('work_mode', '\u2014')}`   "
        f"Alert: `{inverter.get('alert', '\u2014')}`",
        f"Cloudiness: `{_format_cloudiness(weather or {})}`",
    ]

    if bms:
        lines.append("")
        lines.append("*BMS Summary*")
        lines.append(
            f"Packs: `{bms.get('active_count', '\u2014')}`   "
            f"Voltage: `{bms.get('voltage_v', '\u2014')}` V"
        )
        lines.append(
            f"Min cell: `{bms.get('min_cell_v', '\u2014')}` V   "
            f"Max cell: `{bms.get('max_cell_v', '\u2014')}` V   "
            f"Delta: `{bms.get('cell_diff_mv', '\u2014')}` mV"
        )

    return "\n".join(lines)


# -- notification formatters (HTML, bot-initiated messages) --------------------


_SWITCH_REASON_TEXT = {
    "load_shed": "high load (load shed)",
    "overcast": "overcast (100 % cloud)",
    "surplus_ration": "low solar surplus",
    "battery_ration": "battery rationing",
    "alert_restore": "inverter alert",
    "all_clear": "conditions normal",
}


def _fmt_watts(value: float) -> str:
    return f"{value:,.0f} W"


def _fmt_volts(value: float) -> str:
    return f"{value:,.1f} V"


def _fmt_pct(value: float) -> str:
    return f"{value:,.0f} %"


def _fmt_secs(value: float) -> str:
    return f"{value:,.0f} s"


def _format_value(value: Any, formatter: Callable[[float], str]) -> str:
    """Render a numeric payload value, or an em dash placeholder."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "\u2014"
    return formatter(float(value))


_DETAIL_FIELDS = (
    ("Battery", "battery_power_w", _fmt_watts),
    ("PV", "pv_power_w", _fmt_watts),
    ("Grid", "grid_voltage_v", _fmt_volts),
    ("SoC", "battery_soc_pct", _fmt_pct),
)


def _detail_line(payload: dict[str, Any], include_load: bool = False) -> str | None:
    """Build one HTML line of supporting telemetry, if any values exist."""
    fields = _DETAIL_FIELDS
    if include_load:
        fields = (("Load", "load_w", _fmt_watts),) + _DETAIL_FIELDS
    parts = []
    for label, key, formatter in fields:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts.append(f"{label}: <code>{formatter(float(value))}</code>")
    if not parts:
        return None
    return " \u00b7 ".join(parts)


def format_switch_event_message(event: dict[str, Any]) -> str:
    """Build an HTML notification for a switch-bank state change."""
    banks = [str(bank) for bank in event.get("switch_banks", [])]
    bank_text = ", ".join(f"<code>{html.escape(bank)}</code>" for bank in banks)
    reason = str(event.get("reason", "unknown"))
    reason_text = _SWITCH_REASON_TEXT.get(reason, reason)
    if event.get("state") == 0:
        headline = f"{emoji.emojize(':warning:')} <b>Switch bank shed (OFF)</b>"
    else:
        headline = f"{emoji.emojize(':check_mark:')} <b>Switch bank restored (ON)</b>"
    lines = [
        f"{headline}: {bank_text or '<code>unknown</code>'}",
        f"Reason: <code>{html.escape(reason_text)}</code>",
    ]
    details = _detail_line(event, include_load=True)
    if details:
        lines.append(details)
    return "\n".join(lines)


def format_load_warning_message(alert: dict[str, Any]) -> str:
    """Build an HTML notification for a warning threshold crossing."""
    lines = [
        (
            f"{emoji.emojize(':warning:')} <b>Load warning</b> for "
            f"<code>{html.escape(str(DEVICE_NAME_BASE))}</code>: "
            f"<b>{_format_value(alert.get('load_w'), _fmt_watts)}</b> exceeds "
            f"{_format_value(alert.get('threshold_w'), _fmt_watts)}"
        ),
    ]
    details = _detail_line(alert)
    if details:
        lines.append(details)
    return "\n".join(lines)


def format_load_recovery_message(alert: dict[str, Any]) -> str:
    """Build an HTML notification for recovery below the warning threshold."""
    lines = [
        (
            f"{emoji.emojize(':check_mark:')} <b>Load recovered</b> for "
            f"<code>{html.escape(str(DEVICE_NAME_BASE))}</code>: held below "
            f"{_format_value(alert.get('threshold_w'), _fmt_watts)} for "
            f"{_format_value(alert.get('cooldown_secs'), _fmt_secs)}; "
            f"load-shed released"
        ),
    ]
    details = _detail_line(alert)
    if details:
        lines.append(details)
    return "\n".join(lines)


def build_notification_message(payload: dict[str, Any]) -> str | None:
    """Build an HTML notification from a Telegram fan-out payload.

    Returns None for payloads that carry no notification content.
    """
    switch_event = payload.get("switch_event")
    if isinstance(switch_event, dict):
        return format_switch_event_message(switch_event)
    load_alert = payload.get("load_alert")
    if isinstance(load_alert, dict):
        kind = load_alert.get("kind")
        if kind == "load_warning":
            return format_load_warning_message(load_alert)
        if kind == "load_recovery":
            return format_load_recovery_message(load_alert)
    return None


# -- chart rendering (matplotlib) ---------------------------------------------


def _render_line_chart(
    df: pd.DataFrame,
    title: str,
    ylabel: str,
    legend_ncols: int = 1,
    legend_fontsize: str = "medium",
) -> bytes:
    """Render a line chart from a DataFrame with '_time' and numeric columns.

    Returns PNG bytes, or empty bytes if there is no data to plot.
    """
    if df.empty:
        return b""
    time_col = "_time" if "_time" in df.columns else df.columns[0]
    numeric_cols = [
        c for c in df.columns if c != time_col and df[c].dtype in ("float64", "int64")
    ]
    if not numeric_cols:
        return b""

    if df[time_col].dtype == "object":
        try:
            df[time_col] = pd.to_datetime(df[time_col])
        except Exception:
            pass

    df = df.sort_values(by=time_col)

    fig, ax = plt.subplots(figsize=(10, 6))
    for col in numeric_cols:
        ax.plot(df[time_col], df[col], marker=".", label=col)

    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel(ylabel)
    ax.legend(ncols=legend_ncols, fontsize=legend_fontsize)
    ax.grid(True)
    plt.xticks(rotation=45)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close(fig)
    img_bytes = buf.getvalue()
    buf.close()
    return img_bytes


def render_power_chart(df: pd.DataFrame) -> bytes:
    """Render a power-flow line chart as PNG bytes via matplotlib."""
    return _render_line_chart(df, "Power Flows (W)", "Watts")


def render_battery_chart(df: pd.DataFrame) -> bytes:
    """Render a battery-status line chart as PNG bytes via matplotlib."""
    return _render_line_chart(df, "Battery Status", "Value")


def render_cell_chart(df: pd.DataFrame) -> bytes:
    """Render a per-cell voltage line chart as PNG bytes via matplotlib."""
    return _render_line_chart(
        df,
        "Cell Voltages",
        "Volts",
        legend_ncols=4,
        legend_fontsize="small",
    )


# -- caption builder ----------------------------------------------------------


def _build_caption(label: str, df: pd.DataFrame, hours: int) -> str:
    """Build a short plain-text summary caption from a queried DataFrame.

    This is a plain-text caption (no parse_mode) because it appears on
    photo messages.  Any markup would be rendered literally.
    """
    parts: list[str] = [f"{label} -- last {hours} h"]
    if df.empty:
        return "\n".join(parts)
    numeric_cols = [
        c for c in df.columns if c != "_time" and df[c].dtype in ("float64", "int64")
    ]
    if not numeric_cols:
        return "\n".join(parts)
    means = {c: df[c].mean() for c in numeric_cols}
    parts.append("Averages:")
    parts.append(
        "  " + "  ".join(f"{k}: {v:.1f}" for k, v in means.items() if not pd.isna(v))
    )
    return "\n".join(parts)


def build_history_caption(df_power: pd.DataFrame, hours: int) -> str:
    """Build a plain-text caption for the power history chart."""
    return _build_caption("Power history", df_power, hours)


def build_battery_caption(df_battery: pd.DataFrame, hours: int) -> str:
    """Build a plain-text caption for the battery history chart."""
    return _build_caption("Battery history", df_battery, hours)


# -- /imagine caption (plain text) ---------------------------------------------


def build_imagine_caption(
    inverter: dict[str, Any] | None = None,
    weather: dict[str, Any] | None = None,
    switches: dict[str, Any] | None = None,
    load_warning_w: float = DEFAULT_LOAD_WARNING_W,
    load_shed_w: float | None = None,
) -> str:
    """Build a short plain-text caption for the /imagine photo.

    Plain text (no parse mode): the caption rides on a photo message, where
    any markup would be rendered literally.
    """
    inverter = inverter or {}
    weather = weather or {}
    state = load_shed_state(
        inverter,
        switches,
        load_warning_w=load_warning_w,
        load_shed_w=load_shed_w,
    )
    details = ["load shedding active" if state.needed else "no load shedding"]
    soc_pct = numeric_value(inverter.get("battery_soc_pct"))
    if soc_pct is not None:
        details.append(f"battery {soc_pct:.0f} %")
    load_w = numeric_value(inverter.get("total_load_power_w"))
    if load_w is not None:
        details.append(f"load {load_w:,.0f} W")
    cloudiness_pct = numeric_value(weather.get("cloudiness_pct"))
    if cloudiness_pct is not None:
        details.append(f"cloud cover {cloudiness_pct:.0f} %")
    return f"Inverter imagination -- {', '.join(details)}"


def build_imagine_prompt_message(
    scene_name: str,
    aspect_ratio: str,
    prompt: str,
    prompt_tokens: int,
    max_prompt_tokens: int,
) -> str:
    """Build the HTML message that shows the composed /imagine prompt.

    Telegram's HTML parse mode understands only the named entities ``&lt;``,
    ``&gt;``, ``&amp;`` and ``&quot;``; every other character is sent as
    itself (the middle dots below are real ``\\u00b7`` characters so they
    render correctly).
    """
    return (
        f"{emoji.emojize(':artist_palette:')} <b>Scene:</b> "
        f"{html.escape(scene_name)} \u00b7 {html.escape(aspect_ratio)} \u00b7 "
        f"~{prompt_tokens}/{max_prompt_tokens} tokens\n"
        f"<pre>{html.escape(prompt)}</pre>"
    )


# -- cell plot + recommendation (plain-text caption) ---------------------------

# Recommendations are based on the latest observed per-cell delta
CELL_BALANCED_MV = 30.0
CELL_MONITOR_MV = 80.0


def build_cell_recommendation(df: pd.DataFrame) -> str:
    """Summarise the latest cell voltages and recommend an action.

    Uses the last observed value of every cell series, so gaps in individual
    Prometheus series do not blank out the recommendation.

    Returns:
        A plain-text caption for the /cell photo.
    """
    if df.empty:
        return "No cell data available."
    numeric_cols = [
        c for c in df.columns if c != "_time" and df[c].dtype in ("float64", "int64")
    ]
    if not numeric_cols:
        return "No cell data available."
    values: dict[str, float] = {}
    for column in numeric_cols:
        series = df[column].dropna()
        if not series.empty:
            values[column] = float(series.iloc[-1])
    if not values:
        return "No cell data available."
    min_cell = min(values, key=lambda cell: values[cell])
    max_cell = max(values, key=lambda cell: values[cell])
    delta_mv = (values[max_cell] - values[min_cell]) * 1000.0
    lines = [
        f"Cells: {len(values)}  Weakest: {min_cell} {values[min_cell]:.3f} V  "
        f"Strongest: {max_cell} {values[max_cell]:.3f} V",
        f"Delta: {delta_mv:.0f} mV",
    ]
    if delta_mv <= CELL_BALANCED_MV:
        lines.append("Recommendation: cells are well balanced.")
    elif delta_mv <= CELL_MONITOR_MV:
        lines.append(
            f"Recommendation: monitor -- plan a balance charge "
            f"({delta_mv:.0f} mV > {CELL_BALANCED_MV:.0f} mV)."
        )
    else:
        lines.append(
            f"Recommendation: action required -- balance charge and inspect "
            f"connections ({delta_mv:.0f} mV > {CELL_MONITOR_MV:.0f} mV)."
        )
    return "\n".join(lines)
