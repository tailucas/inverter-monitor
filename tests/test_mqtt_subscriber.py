#!/usr/bin/env python
"""Unit tests for the switch-bank control publish path of `MqttSubscriber`.

Covers the structured INFO record emitted for every switch-bank control
message (the MQTT publish that drives load shedding) and the change gating
that keeps an unchanged decision from re-publishing.

Importing ``app.__main__`` reads ``[metrics] debug_csv`` at module scope
without a fallback, so the configuration section is seeded before the
import.
"""

import importlib
import json
import logging
from typing import Any

import pytest
from tailucas_pylib import APP_NAME, app_config

if not app_config.has_section("metrics"):
    app_config.add_section("metrics")
if not app_config.has_option("metrics", "debug_csv"):
    app_config.set("metrics", "debug_csv", "")

app_module = importlib.import_module("app.__main__")

# the switch-bank control topic is built from the configured prefix
TOPIC_PREFIX = "inverter"
BANK = "bank1"
CONTROL_TOPIC = f"{TOPIC_PREFIX}/control/{BANK}"


class FakeMqttClient:
    """MQTT client stand-in recording every publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append({"topic": topic, "payload": payload})


@pytest.fixture
def subscriber() -> Any:
    """A MqttSubscriber wired to a recording client and one bank."""
    subscriber = app_module.MqttSubscriber(
        mqtt_server_address="localhost",
        mqtt_topic_prefix=TOPIC_PREFIX,
        mqtt_switch_devices=[BANK],
    )
    subscriber._switch_state = {BANK: [1, 1]}
    subscriber._mqtt_client = FakeMqttClient()
    return subscriber


class TestSwitchBankControlPublish:
    """A shed decision publishes the control message and logs topic+payload."""

    def test_publish_logs_topic_and_payload(
        self, subscriber: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            changed_banks = subscriber.set_switch_state(
                switch_state=0, reason="load_shed"
            )

        assert changed_banks == [BANK]
        client = subscriber._mqtt_client
        assert [p["topic"] for p in client.published] == [CONTROL_TOPIC]
        published = json.loads(client.published[0]["payload"])
        assert published["state"] == [0, 0]
        assert published["traceparent"].startswith("00-")
        records = [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control message published"
        ]
        assert len(records) == 1
        record: Any = records[0]
        assert record.levelno == logging.INFO
        assert record.topic == CONTROL_TOPIC
        assert record.payload == client.published[0]["payload"]
        assert record.switch_bank == BANK
        assert record.switch_state == 0
        assert record.reason == "load_shed"
        assert record.message_bytes == len(record.payload)

    def test_unchanged_state_does_not_publish_or_log(
        self, subscriber: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber._switch_state = {BANK: [0, 0]}
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            changed_banks = subscriber.set_switch_state(
                switch_state=0, reason="load_shed"
            )

        assert changed_banks == []
        assert subscriber._mqtt_client.published == []
        assert [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control message published"
        ] == []
