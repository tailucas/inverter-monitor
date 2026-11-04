#!/usr/bin/env python
"""Unit tests for the load-alert monitor's PagerDuty trigger path.

The high-load incident class must page at high urgency. Events API v2 has no
urgency field of its own: PagerDuty derives incident urgency from the event's
payload severity, where ``critical``/``error`` map to high urgency and
``warning``/``info`` map to low. This pins the severity the monitor sends for
the ``load_high`` incident.

Importing ``app.__main__`` reads ``[metrics] debug_csv`` at module scope
without a fallback, so the configuration sections are seeded before the
import.
"""

import importlib
import logging
from typing import Any

import pytest
from tailucas_pylib import APP_NAME, app_config

if not app_config.has_section("metrics"):
    app_config.add_section("metrics")
if not app_config.has_option("metrics", "debug_csv"):
    app_config.set("metrics", "debug_csv", "")
if not app_config.has_section("app"):
    app_config.add_section("app")
if not app_config.has_option("app", "paging_enabled"):
    app_config.set("app", "paging_enabled", "false")

app_module = importlib.import_module("app.__main__")

TRIGGER_MESSAGE = "PagerDuty alert triggered for high load"


class FakePagerDutyClient:
    """Events API v2 client stand-in recording trigger/resolve calls."""

    def __init__(self) -> None:
        self.triggered: list[dict[str, Any]] = []
        self.resolved: list[str] = []

    def trigger(self, **kwargs: Any) -> str:
        self.triggered.append(kwargs)
        dedup_key = kwargs.get("dedup_key")
        return dedup_key if isinstance(dedup_key, str) else "generated-dedup-key"

    def resolve(self, dedup_key: str) -> str:
        self.resolved.append(dedup_key)
        return dedup_key


@pytest.fixture
def monitor() -> Any:
    """A LoadAlertMonitor wired to a recording PagerDuty client."""
    load_alert_monitor = app_module.LoadAlertMonitor(telegram_enabled=False)
    load_alert_monitor.pd_client = FakePagerDutyClient()
    return load_alert_monitor


class TestHighLoadPagerDutyTrigger:
    """The ``load_high`` incident pages at high urgency."""

    def test_trigger_uses_critical_severity(
        self, monitor: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        details = {"timestamp": 1_700_000_000.0, "soc_pct": 55}
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            monitor._trigger_pd(load_w=7600.5, details=details)

        client = monitor.pd_client
        assert len(client.triggered) == 1
        trigger = client.triggered[0]
        # `critical` is what makes PagerDuty raise a high-urgency incident;
        # the event payload has no urgency field to set directly.
        assert trigger["severity"] == app_module.PD_LOAD_SEVERITY
        assert trigger["severity"] == "critical"
        assert trigger["dedup_key"] == app_module.PD_LOAD_DEDUP_KEY
        assert trigger["custom_details"] == {
            "load_w": 7600.5,
            "threshold_w": monitor._load_critical_w,
            "soc_pct": 55,
        }

        records = [r for r in caplog.records if r.getMessage() == TRIGGER_MESSAGE]
        assert len(records) == 1
        record: Any = records[0]
        assert record.levelno == logging.INFO
        assert record.dedup_key == app_module.PD_LOAD_DEDUP_KEY
        assert record.severity == "critical"

    def test_trigger_marks_the_incident_as_reported(self, monitor: Any) -> None:
        monitor._trigger_pd(load_w=8000.0, details={"timestamp": 1.0})
        # a successful trigger must not be retried on the next sample
        decision = monitor._evaluator.evaluate(load_w=8000.0, now=1.0)
        assert decision.pd_trigger is False
