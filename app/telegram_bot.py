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


def format_status_message(inverter: dict[str, Any], bms_summary: dict[str, Any]) -> str:
    """Build a compact Markdown status message from inverter data and BMS summary.

    Args:
        inverter: A dict of scalar inverter telemetry fields.
        bms_summary: A dict of BMS summary fields (e.g. ``active_count``,
            ``voltage_v``, ``min_cell_v``, ``max_cell_v``, ``cell_diff_mv``).

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
    ax.legend()
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


# -- caption builder ----------------------------------------------------------


def build_history_caption(
    df_power: pd.DataFrame,
    df_battery: pd.DataFrame,
    hours: int,
) -> str:
    """Build a short plain-text summary caption from the queried DataFrames.

    This is a plain-text caption (no parse_mode) because it appears on
    photo messages.  Any markup would be rendered literally.
    """
    parts: list[str] = [f"History -- last {hours} h"]
    for df, label in [(df_power, "Power"), (df_battery, "Battery")]:
        if df.empty:
            continue
        numeric_cols = [
            c
            for c in df.columns
            if c != "_time" and df[c].dtype in ("float64", "int64")
        ]
        if not numeric_cols:
            continue
        means = {c: df[c].mean() for c in numeric_cols}
        parts.append(f"\n{label} averages:")
        parts.append(
            "  "
            + "  ".join(f"{k}: {v:.1f}" for k, v in means.items() if not pd.isna(v))
        )
    return "\n".join(parts)
