#!/usr/bin/env python
"""Pure helper functions and data structures for the Telegram bot.

Status buffer, Markdown status formatter, matplotlib chart renderers,
and plain-text caption builder. No Telegram library imports, no async
handlers, no AppThread -- those live in `app.bot`.
"""

import io
from threading import Lock
from typing import Any

import matplotlib
import pandas as pd
from tailucas_pylib import APP_NAME, app_config

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
