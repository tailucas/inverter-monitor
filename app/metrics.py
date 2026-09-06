#!/usr/bin/env python
"""Prometheus metrics client for inverter-monitor.

Follows net-tool's pattern: module-level globals + configure() called
from main() before the event loop, typed DTO return from fetch_metrics().
"""

import time
from dataclasses import dataclass

import requests
from tailucas_pylib import log


@dataclass
class PrometheusMetricDTO:
    """A single time-series data point from Prometheus."""

    ts_ms: float
    value: float
    metric_name: str


# Module-level globals populated by configure()
_url = ""
_user = ""
_token = ""


# PromQL query sets for inverter-monitor OTEL gauges
POWER_QUERIES = {
    "total_power_w": "inverter_total_power_w",
    "total_load_power_w": "inverter_total_load_power_w",
    "battery_power_w": "inverter_battery_power_w",
    "total_grid_power_w": "inverter_total_grid_power_w",
    "pv1_power_w": "inverter_pv1_power_w",
    "pv2_power_w": "inverter_pv2_power_w",
}

BATTERY_QUERIES = {
    "battery_soc_pct": "inverter_battery_soc_pct",
    "battery_voltage_v": "inverter_battery_voltage_v",
    "battery_current_a": "inverter_battery_current_a",
}


def configure(url: str = "", user: str = "", token: str = "") -> None:
    """Store Prometheus connection details.

    Called from main thread before the event loop starts.
    """
    global _url, _user, _token
    _url = url
    _user = user
    _token = token


def _query_range(
    metric_name: str,
    promql: str,
    start: int,
    end: int,
    step: str = "5m",
) -> list[PrometheusMetricDTO]:
    """Execute a single PromQL range query and return parsed data points."""
    global _url, _user, _token
    if not _url:
        log.warning(
            "Prometheus URL is not configured. Cannot fetch metric.",
            extra={"metric_name": metric_name},
        )
        return []

    # Build the correct URL path for Grafana Cloud Mimir / Prometheus
    base = _url.rstrip("/")
    if not base.endswith("/prometheus/api/v1/query_range"):
        base += "/prometheus/api/v1/query_range"

    params: dict[str, str | int] = {
        "query": promql,
        "start": start,
        "end": end,
        "step": step,
    }

    auth = None
    if _user and _token:
        auth = (_user, _token)

    headers = {"Accept": "application/json"}

    log.debug(
        "Querying Prometheus range",
        extra={
            "metric_name": metric_name,
            "start": start,
            "end": end,
            "step": step,
        },
    )

    response = requests.get(base, params=params, auth=auth, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "success":
        raise ValueError(f"Prometheus query failed for {metric_name}: {data}")

    results: list[PrometheusMetricDTO] = []
    result_list = data.get("data", {}).get("result", [])
    for item in result_list:
        values = item.get("values", [])
        for ts_val in values:
            ts = float(ts_val[0])
            val = float(ts_val[1]) if ts_val[1] != "NaN" else 0.0
            results.append(
                PrometheusMetricDTO(
                    ts_ms=ts * 1000.0,
                    value=val,
                    metric_name=metric_name,
                )
            )
    return results


def fetch_metrics(
    hours: int = 24,
    query_set: dict[str, str] | None = None,
) -> list[PrometheusMetricDTO]:
    """Fetch time-series metrics from Prometheus for the given hours.

    Args:
        hours: Number of hours to look back.
        query_set: Dict of {friendly_name: PromQL_metric_name}.
            Defaults to POWER_QUERIES | BATTERY_QUERIES.

    Returns:
        List of PrometheusMetricDTO sorted by timestamp.
    """
    if query_set is None:
        query_set = {**POWER_QUERIES, **BATTERY_QUERIES}

    end_time = int(time.time())
    start_time = end_time - hours * 3600

    all_results: list[PrometheusMetricDTO] = []
    for friendly_name, promql in query_set.items():
        try:
            results = _query_range(
                metric_name=friendly_name,
                promql=promql,
                start=start_time,
                end=end_time,
            )
            all_results.extend(results)
        except Exception as exc:
            log.warning(
                "Prometheus query failed for metric",
                extra={
                    "metric_name": friendly_name,
                    "promql": promql,
                    "error": str(exc),
                },
            )

    # Sort by timestamp
    all_results.sort(key=lambda r: r.ts_ms)
    return all_results
