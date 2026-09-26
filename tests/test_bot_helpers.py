#!/usr/bin/env python
"""Unit tests for pure Telegram handler helpers (DataFrame shaping)."""

from app.bot import _to_df
from app.metrics import PrometheusMetricDTO


def test_to_df_label_columns() -> None:
    """Cell series are keyed by pack and cell labels."""
    results = [
        PrometheusMetricDTO(
            ts_ms=1000.0,
            value=3.30,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "01"},
        ),
        PrometheusMetricDTO(
            ts_ms=1000.0,
            value=3.28,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "02"},
        ),
        PrometheusMetricDTO(
            ts_ms=2000.0,
            value=3.31,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "01"},
        ),
    ]
    df = _to_df(results, use_labels=True)
    assert set(df.columns) == {"_time", "BMS01 c01", "BMS01 c02"}
    assert df["BMS01 c01"].tolist() == [3.30, 3.31]


def test_to_df_metric_columns() -> None:
    """Unlabeled series keep the friendly metric name as the column."""
    results = [
        PrometheusMetricDTO(ts_ms=1000.0, value=2000.0, metric_name="total_power_w"),
        PrometheusMetricDTO(ts_ms=2000.0, value=2100.0, metric_name="total_power_w"),
    ]
    df = _to_df(results)
    assert list(df.columns) == ["_time", "total_power_w"]
    assert df["total_power_w"].tolist() == [2000.0, 2100.0]


def test_to_df_empty() -> None:
    """No results yields an empty DataFrame."""
    assert _to_df([]).empty
