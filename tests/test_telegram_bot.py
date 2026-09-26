#!/usr/bin/env python
"""Unit tests for the Telegram bot module — pure functions only."""

import re
from typing import Any

import pandas as pd
import pytest

from app.metrics import (
    BATTERY_QUERIES,
    POWER_QUERIES,
    configure,
    fetch_metrics,
)
from app.telegram_bot import (
    BmsSummaryBuffer,
    SwitchStatsBuffer,
    WeatherBuffer,
    build_battery_caption,
    build_bms_summary,
    build_cell_recommendation,
    build_history_caption,
    build_imagine_caption,
    build_imagine_prompt_message,
    build_notification_message,
    format_load_recovery_message,
    format_load_warning_message,
    format_status_message,
    format_switch_event_message,
    render_battery_chart,
    render_cell_chart,
    render_power_chart,
)


@pytest.fixture
def sample_inverter() -> dict[str, Any]:
    return {
        "battery_soc_pct": 72.3,
        "battery_voltage_v": 51.2,
        "battery_power_w": -850.0,
        "pv1_power_w": 1200.0,
        "pv2_power_w": 800.0,
        "total_power_w": 2000.0,
        "total_load_power_w": 950.0,
        "grid_voltage_l1_v": 230.5,
        "grid_voltage_l2_v": 0.0,
        "daily_production_kwh": 18.4,
        "daily_load_consumption_kwh": 12.1,
        "battery_current_a": -16.5,
        "work_mode": 1,
        "alert": 0,
    }


@pytest.fixture
def sample_bms_summary() -> dict[str, Any]:
    return {
        "active_count": 2,
        "voltage_v": 51.2,
        "min_cell_v": 3.15,
        "max_cell_v": 3.22,
        "cell_diff_mv": 70,
    }


def test_bms_summary_buffer_empty() -> None:
    """A fresh buffer returns empty dict."""
    buf = BmsSummaryBuffer()
    assert buf.summary() == {}


def test_bms_summary_buffer_store_and_copy() -> None:
    """Verify BmsSummaryBuffer stores keys and returns a copy on read."""
    buf = BmsSummaryBuffer()
    data = {"active_count": 2, "voltage_v": 51.2}
    buf.update(data)

    result = buf.summary()
    assert result == data
    # Verify copy semantics: mutating the returned dict does not affect buffer
    result["active_count"] = 99
    assert buf.summary()["active_count"] == 2


def test_weather_buffer_store_and_copy() -> None:
    """Verify WeatherBuffer stores keys and returns a copy on read."""
    buf = WeatherBuffer()
    assert buf.summary() == {}
    data = {"cloudiness_pct": 62, "midday_pct": 40}
    buf.update(data)
    result = buf.summary()
    assert result == data
    result["cloudiness_pct"] = 100
    assert buf.summary()["cloudiness_pct"] == 62


def test_switch_stats_buffer_store_and_copy() -> None:
    """Verify SwitchStatsBuffer stores keys and returns a copy on read."""
    buf = SwitchStatsBuffer()
    assert buf.summary() == {}
    data = {"load_shed": 1, "overcast": 0, "switch_state": 0}
    buf.update(data)
    result = buf.summary()
    assert result == data
    result["load_shed"] = 0
    assert buf.summary()["load_shed"] == 1


def test_build_imagine_caption_reports_load_shedding() -> None:
    """The /imagine caption summarises the shed state and live readings."""
    caption = build_imagine_caption(
        {"battery_soc_pct": 61.4, "total_load_power_w": 7240.0},
        {"cloudiness_pct": 100.2},
        {"load_shed": 1},
    )
    assert "load shedding active" in caption
    assert "battery 61 %" in caption
    assert "load 7,240 W" in caption
    assert "cloud cover 100 %" in caption


def test_build_imagine_caption_without_data() -> None:
    """A caption is always produced, even with no telemetry."""
    caption = build_imagine_caption()
    assert caption.startswith("Inverter imagination --")
    assert "no load shedding" in caption


def test_build_imagine_caption_uses_ratio_threshold() -> None:
    """The battery-ration latch forces the shedding wording."""
    caption = build_imagine_caption(None, None, {"battery_ration": 1})
    assert "load shedding active" in caption


def test_build_imagine_prompt_message_uses_telegram_html() -> None:
    """Only Telegram-supported entities appear and the prompt is escaped."""
    message = build_imagine_prompt_message(
        "orbital_station", "9:16", "A <b>bold</b> & risky prompt", 350, 480
    )
    assert "<b>Scene:</b> orbital_station" in message
    assert "\u00b7 9:16 \u00b7 ~350/480 tokens" in message
    assert "<pre>A &lt;b&gt;bold&lt;/b&gt; &amp; risky prompt</pre>" in message
    assert "&middot;" not in message
    entities = set(re.findall(r"&[a-zA-Z]+;", message))
    assert entities <= {"&lt;", "&gt;", "&amp;", "&quot;"}


def test_build_bms_summary() -> None:
    """Verify build_bms_summary derives correct fields from battery payloads."""
    battery_items = [
        {
            "labels": {"bms_addr": "0x01"},
            "metrics": {"voltage_v": 51.2, "min_cell_v": 3.15, "cell_diff_mv": 70},
        },
        {
            "labels": {"bms_addr": "0x02"},
            "metrics": {"voltage_v": 51.0, "max_cell_v": 3.22, "cell_diff_mv": 80},
        },
    ]
    summary = build_bms_summary(battery_items)
    assert summary["active_count"] == 2
    assert summary["voltage_v"] == 51.2  # first entry wins
    assert summary["min_cell_v"] == 3.15
    assert summary["max_cell_v"] == 3.22
    assert summary["cell_diff_mv"] == 70  # first entry wins


def test_build_bms_summary_empty() -> None:
    """Empty battery items produce only active_count."""
    summary = build_bms_summary([])
    assert summary == {"active_count": 0}


def test_format_status_message(sample_inverter: dict[str, Any]) -> None:
    """Verify status message contains expected fields."""
    msg = format_status_message(
        inverter=sample_inverter,
        bms_summary={"active_count": 2},
    )
    assert "SOC: `72.3 %`" in msg
    assert "PV1: `1200.0 W`" in msg
    assert "PV2: `800.0 W`" in msg
    assert "Load: `950.0 W`" in msg
    assert "Grid: `230.5 V`" in msg
    assert "*Inverter Status*" in msg
    # No "Last update" / "No data" lines since inverter is always live
    assert "ago" not in msg
    assert "No data" not in msg


def test_format_status_message_empty() -> None:
    """Verify empty inverter yields appropriate message."""
    msg = format_status_message(inverter={}, bms_summary={})
    assert "*Inverter Status*" in msg
    # Every field shows the em dash placeholder
    assert "\u2014" in msg
    assert "ago" not in msg
    assert "No data" not in msg


def test_format_status_message_with_bms(
    sample_inverter: dict[str, Any],
    sample_bms_summary: dict[str, Any],
) -> None:
    """Verify BMS details appear in status message."""
    msg = format_status_message(
        inverter=sample_inverter,
        bms_summary=sample_bms_summary,
    )
    assert "*BMS Summary*" in msg
    assert "Packs: `2`" in msg
    assert "Min cell: `3.15` V" in msg
    assert "Max cell: `3.22` V" in msg
    assert "Delta: `70` mV" in msg


def test_format_status_message_with_weather(
    sample_inverter: dict[str, Any],
) -> None:
    """Verify the latest cloudiness is decorated in the status message."""
    msg = format_status_message(
        inverter=sample_inverter,
        bms_summary={},
        weather={"cloudiness_pct": 62, "midday_pct": 40},
    )
    assert "Cloudiness: `62%`" in msg
    overcast = format_status_message(
        inverter=sample_inverter,
        bms_summary={},
        weather={"cloudiness_pct": 100},
    )
    assert "Cloudiness: `100%`" in overcast


def test_format_status_message_without_weather(
    sample_inverter: dict[str, Any],
) -> None:
    """Missing weather data renders the em dash placeholder."""
    msg = format_status_message(inverter=sample_inverter, bms_summary={})
    assert "Cloudiness: `\u2014`" in msg


def test_metrics_configure_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _query_range builds correct URL, params, auth, parses response."""
    import requests

    configure(url="https://prometheus.example.com", user="testuser", token="testtoken")

    captured: dict[str, Any] = {"url": "", "auth": None, "params": {}}

    def mock_get(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["auth"] = kwargs.get("auth")
        captured["params"] = kwargs.get("params", {})
        return _fake_prometheus_response()

    monkeypatch.setattr(requests, "get", mock_get)

    from app.metrics import _query_range

    results = _query_range(
        metric_name="total_power_w",
        promql="inverter_total_power_w",
        start=1700000000,
        end=1700086400,
    )

    # URL verification
    url = captured["url"]
    assert isinstance(url, str) and url.endswith("/prometheus/api/v1/query_range")
    params = captured["params"]
    assert isinstance(params, dict)
    assert params.get("query") == "inverter_total_power_w"
    assert params.get("start") == 1700000000
    assert params.get("step") == "5m"
    # Basic auth
    assert captured["auth"] == ("testuser", "testtoken")
    # Parsing verification
    assert len(results) == 3
    assert results[0].metric_name == "total_power_w"
    assert results[0].value == 2000.0
    # NaN is converted to 0.0 (net-tool pattern)
    assert results[1].value == 0.0
    assert results[2].value == 1950.0
    # Timestamps are in order
    assert results[2].ts_ms > results[1].ts_ms
    # Series labels are captured (dropping __name__)
    assert results[0].labels == {"bms_addr": "BMS01", "cell": "01"}


def _fake_prometheus_response():
    """Return a fake requests.Response for a successful Prometheus query."""

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {
                                "__name__": "inverter_total_power_w",
                                "bms_addr": "BMS01",
                                "cell": "01",
                            },
                            "values": [
                                [1700000000.0, "2000.0"],
                                [1700000300.0, "NaN"],
                                [1700000600.0, "1950.0"],
                            ],
                        }
                    ]
                },
            }

    return FakeResponse()


def test_metrics_query_sets_configured() -> None:
    """Verify POWER_QUERIES and BATTERY_QUERIES have expected keys."""
    assert "total_power_w" in POWER_QUERIES
    assert "battery_soc_pct" in BATTERY_QUERIES
    assert len(POWER_QUERIES) == 6
    assert len(BATTERY_QUERIES) == 3


def test_fetch_metrics_empty_url() -> None:
    """fetch_metrics returns empty list when URL is not configured."""
    configure(url="", user="", token="")
    results = fetch_metrics(hours=1)
    assert results == []


def test_render_power_chart_empty() -> None:
    """Empty DataFrame produces empty bytes."""
    result = render_power_chart(pd.DataFrame())
    assert result == b""


def test_render_battery_chart_empty() -> None:
    """Empty DataFrame produces empty bytes."""
    result = render_battery_chart(pd.DataFrame())
    assert result == b""


def test_build_history_caption() -> None:
    """Verify the power caption contains the expected averages."""
    df_power = pd.DataFrame(
        {
            "_time": pd.date_range("2026-09-03", periods=3, freq="h"),
            "total_power_w": [2000.0, 2100.0, 1900.0],
            "total_load_power_w": [950.0, 970.0, 930.0],
        }
    )
    caption = build_history_caption(df_power, hours=24)
    assert "Power history" in caption
    assert "24 h" in caption
    assert "total_power_w: 2000.0" in caption


def test_build_battery_caption() -> None:
    """Verify the battery caption contains the expected averages."""
    df_battery = pd.DataFrame(
        {
            "_time": pd.date_range("2026-09-03", periods=3, freq="h"),
            "battery_soc_pct": [72.0, 71.0, 70.5],
        }
    )
    caption = build_battery_caption(df_battery, hours=12)
    assert "Battery history" in caption
    assert "12 h" in caption
    assert "battery_soc_pct: 71.2" in caption


def test_build_captions_empty() -> None:
    """Empty DataFrames produce a minimal caption."""
    history = build_history_caption(pd.DataFrame(), hours=12)
    assert "Power history" in history
    assert "12 h" in history
    battery = build_battery_caption(pd.DataFrame(), hours=6)
    assert "Battery history" in battery


def _cell_frame(delta_mv: float) -> pd.DataFrame:
    """Build a two-series cell DataFrame separated by delta_mv."""
    return pd.DataFrame(
        {
            "_time": pd.date_range("2026-09-26", periods=3, freq="h"),
            "BMS01 c01": [3.300, 3.300, 3.300],
            "BMS01 c02": [3.300, 3.300, 3.300 + delta_mv / 1000.0],
        }
    )


def test_build_cell_recommendation_balanced() -> None:
    """A small delta recommends no action."""
    caption = build_cell_recommendation(_cell_frame(20.0))
    assert "Cells: 2" in caption
    assert "Weakest: BMS01 c01 3.300 V" in caption
    assert "Delta: 20 mV" in caption
    assert "cells are well balanced" in caption


def test_build_cell_recommendation_monitor() -> None:
    """A moderate delta recommends monitoring."""
    caption = build_cell_recommendation(_cell_frame(50.0))
    assert "Delta: 50 mV" in caption
    assert "monitor" in caption


def test_build_cell_recommendation_action() -> None:
    """A large delta recommends action."""
    caption = build_cell_recommendation(_cell_frame(120.0))
    assert "Delta: 120 mV" in caption
    assert "action required" in caption


def test_build_cell_recommendation_no_data() -> None:
    """Empty cell data yields a clear fallback caption."""
    assert "No cell data available" in build_cell_recommendation(pd.DataFrame())


def test_render_cell_chart_empty() -> None:
    """Empty cell DataFrame produces empty bytes."""
    assert render_cell_chart(pd.DataFrame()) == b""


def test_format_switch_event_message_shed() -> None:
    """Switch event formatter renders the action, reason and supporting data."""
    msg = format_switch_event_message(
        {
            "switch_banks": ["bank1"],
            "state": 0,
            "reason": "load_shed",
            "load_w": 7240.0,
            "battery_power_w": 2900.0,
            "pv_power_w": 4200.0,
            "grid_voltage_v": 0.0,
            "battery_soc_pct": 61.0,
        }
    )
    assert "Switch bank shed (OFF)" in msg
    assert "high load (load shed)" in msg
    assert "7,240 W" in msg
    assert "61 %" in msg


def test_format_switch_event_message_restore() -> None:
    """Switch event formatter renders a restore with multiple banks."""
    msg = format_switch_event_message(
        {"switch_banks": ["bank1", "bank2"], "state": 1, "reason": "all_clear"}
    )
    assert "Switch bank restored (ON)" in msg
    assert "conditions normal" in msg
    assert "<code>bank1</code>, <code>bank2</code>" in msg


def test_format_switch_event_message_escapes_bank_name() -> None:
    """Bank names from MQTT topics are HTML-escaped in the notification."""
    msg = format_switch_event_message(
        {"switch_banks": ["<b>evil</b>"], "state": 0, "reason": "load_shed"}
    )
    assert "&lt;b&gt;evil&lt;/b&gt;" in msg
    assert "<b>evil</b>" not in msg


def test_format_load_warning_message() -> None:
    """Load warning formatter renders the load, threshold and details."""
    msg = format_load_warning_message(
        {
            "kind": "load_warning",
            "load_w": 7240.0,
            "threshold_w": 7000.0,
            "cooldown_secs": 600.0,
            "battery_soc_pct": 61.0,
            "battery_power_w": 2900.0,
            "pv_power_w": 4200.0,
            "grid_voltage_v": 0.0,
        }
    )
    assert "Load warning" in msg
    assert "7,240 W" in msg
    assert "7,000 W" in msg
    assert "SoC: <code>61 %</code>" in msg


def test_format_load_recovery_message() -> None:
    """Load recovery formatter reports the cooldown-aligned release."""
    msg = format_load_recovery_message(
        {
            "kind": "load_recovery",
            "load_w": 6400.0,
            "threshold_w": 7000.0,
            "cooldown_secs": 600.0,
        }
    )
    assert "Load recovered" in msg
    assert "600 s" in msg
    assert "load-shed released" in msg


def test_build_notification_message_dispatch() -> None:
    """The dispatcher routes notification payloads and rejects others."""
    warning = build_notification_message(
        {
            "load_alert": {
                "kind": "load_warning",
                "load_w": 7240.0,
                "threshold_w": 7000.0,
            }
        }
    )
    assert warning is not None
    assert "Load warning" in warning

    switch = build_notification_message(
        {
            "switch_event": {
                "switch_banks": ["bank1"],
                "state": 0,
                "reason": "load_shed",
            }
        }
    )
    assert switch is not None
    assert "Switch bank shed (OFF)" in switch

    assert build_notification_message({}) is None
    assert build_notification_message({"battery": []}) is None
    assert build_notification_message({"load_alert": {"kind": "other"}}) is None


if __name__ == "__main__":
    pytest.main(["-v", __file__])
