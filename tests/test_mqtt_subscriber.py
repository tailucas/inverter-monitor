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
from types import SimpleNamespace
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
# emitted once per bank and episode for a bank that never reported state
MISSING_STATE_MESSAGE = "Switch banks without reported state were not controlled"


class FakeMqttClient:
    """MQTT client stand-in recording every publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append({"topic": topic, "payload": payload})


class FakeAppSocket:
    """ZMQ socket stand-in recording fanned-out payloads."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_pyobj(self, payload: Any) -> None:
        self.sent.append(payload)


def _state_message(bank: str, switches: list[int]) -> Any:
    """A minimal paho-style MQTT message for a switch-bank state publish."""
    return SimpleNamespace(
        topic=f"{TOPIC_PREFIX}/state/{bank}",
        payload=json.dumps({"switches": switches}).encode(),
    )


def _new_subscriber(banks: list[str]) -> Any:
    """A MqttSubscriber with a recording client and no reported bank state."""
    subscriber = app_module.MqttSubscriber(
        mqtt_server_address="localhost",
        mqtt_topic_prefix=TOPIC_PREFIX,
        mqtt_switch_devices=banks,
    )
    subscriber._mqtt_client = FakeMqttClient()
    return subscriber


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


class TestMissingSwitchStateDiagnostics:
    """Configured banks that never reported state are loudly left alone."""

    def test_unreported_bank_warns_once_and_never_publishes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            first = subscriber.set_switch_state(switch_state=0, reason="load_shed")
            second = subscriber.set_switch_state(switch_state=0, reason="load_shed")

        assert first == []
        assert second == []
        assert subscriber._mqtt_client.published == []
        records = [r for r in caplog.records if r.getMessage() == MISSING_STATE_MESSAGE]
        assert len(records) == 1
        record: Any = records[0]
        assert record.levelno == logging.WARNING
        assert record.switch_state == 0
        assert record.reason == "load_shed"
        assert record.subscription_topic == f"{TOPIC_PREFIX}/state/#"
        assert record.configured_banks == [BANK]
        assert record.unreported_banks == [BANK]
        assert record.state_age_secs is None
        assert "error_hint" in record.__dict__

    def test_reported_bank_is_controlled_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber.on_message(None, None, _state_message(BANK, [1, 1]))
            changed_banks = subscriber.set_switch_state(
                switch_state=0, reason="load_shed"
            )

        assert changed_banks == [BANK]
        assert [p["topic"] for p in subscriber._mqtt_client.published] == [
            CONTROL_TOPIC
        ]
        assert [
            r for r in caplog.records if r.getMessage() == MISSING_STATE_MESSAGE
        ] == []

    def test_state_message_logs_the_learned_bank(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber.on_message(None, None, _state_message(BANK, [1, 0]))

        records = [
            r for r in caplog.records if r.getMessage() == "Switch bank state received"
        ]
        assert len(records) == 1
        record: Any = records[0]
        assert record.switch_bank == BANK
        assert record.switch_count == 2
        assert record.state == [1, 0]

    def test_partial_state_feed_warns_only_about_the_silent_bank(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK, "bank2"])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber.on_message(None, None, _state_message(BANK, [1]))
            subscriber.set_switch_state(switch_state=0, reason="load_shed")
            subscriber.set_switch_state(switch_state=0, reason="load_shed")

        records = [r for r in caplog.records if r.getMessage() == MISSING_STATE_MESSAGE]
        assert len(records) == 1
        record: Any = records[0]
        assert record.unreported_banks == ["bank2"]
        assert record.state_age_secs is not None


class TestManualSwitchCommandDiagnostics:
    """The manual command record shows what the MQTT banks actually did."""

    def test_manual_command_reports_unreported_banks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber._apply_manual_switch_command(
                app_socket=FakeAppSocket(), command="start_load_shed"
            )

        records = [
            r
            for r in caplog.records
            if r.getMessage() == "Manual switch command applied"
        ]
        assert len(records) == 1
        record: Any = records[0]
        assert record.command == "start_load_shed"
        assert record.switch_state == 0
        assert record.switch_banks == []
        assert record.configured_banks == [BANK]
        assert record.known_banks == []
        assert record.unchanged_banks == []
        assert record.state_age_secs is None
        assert record.load_shed == 1

    def test_manual_command_reports_banks_already_at_the_target(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        subscriber.on_message(None, None, _state_message(BANK, [0, 0]))
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber._apply_manual_switch_command(
                app_socket=FakeAppSocket(), command="start_load_shed"
            )

        assert subscriber._mqtt_client.published == []
        records = [
            r
            for r in caplog.records
            if r.getMessage() == "Manual switch command applied"
        ]
        assert len(records) == 1
        record: Any = records[0]
        assert record.switch_banks == []
        assert record.known_banks == [BANK]
        assert record.unchanged_banks == [BANK]
        assert isinstance(record.state_age_secs, float)


class TestConfiguredBankParsing:
    """Configured bank names survive padded CSV entries."""

    def test_padded_bank_names_are_trimmed_and_blanks_dropped(self) -> None:
        subscriber = _new_subscriber(banks=[" bank1 ", "", " bank2 "])
        assert subscriber._mqtt_switch_devices == ["bank1", "bank2"]
