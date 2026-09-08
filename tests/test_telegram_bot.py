#!/usr/bin/env python
"""Unit tests for the Telegram bot module — pure functions only."""

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
    build_bms_summary,
    build_history_caption,
    format_status_message,
    render_battery_chart,
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
                            "metric": {},
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
    """Verify caption contains expected sections."""
    df_power = pd.DataFrame(
        {
            "_time": pd.date_range("2026-09-03", periods=3, freq="h"),
            "total_power_w": [2000.0, 2100.0, 1900.0],
            "total_load_power_w": [950.0, 970.0, 930.0],
        }
    )
    df_battery = pd.DataFrame(
        {
            "_time": pd.date_range("2026-09-03", periods=3, freq="h"),
            "battery_soc_pct": [72.0, 71.0, 70.5],
        }
    )
    caption = build_history_caption(df_power, df_battery, hours=24)
    assert "History" in caption
    assert "24 h" in caption
    assert "Power averages" in caption
    assert "Battery averages" in caption


def test_build_history_caption_empty() -> None:
    """Empty DataFrames produce a minimal caption."""
    caption = build_history_caption(pd.DataFrame(), pd.DataFrame(), hours=12)
    assert "History" in caption
    assert "12 h" in caption


if __name__ == "__main__":
    pytest.main(["-v", __file__])
