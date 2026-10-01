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
import time
from types import SimpleNamespace
from typing import Any

import pytest
from tailucas_pylib import APP_NAME, app_config

from app.load_alerts import SwitchConditionConfig

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


class TestSwitchCommandRetryGate:
    """A command the bank has not followed is retried at a bounded rate."""

    def _subscriber_with_bank_off(self) -> Any:
        """A subscriber whose only bank reports both switches off."""
        subscriber = _new_subscriber(banks=[BANK])
        subscriber.on_message(None, None, _state_message(BANK, [0, 0]))
        return subscriber

    def test_same_command_is_published_once_then_deferred(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = self._subscriber_with_bank_off()
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            first = subscriber.set_switch_state(switch_state=1, reason="all_clear")
            second = subscriber.set_switch_state(switch_state=1, reason="all_clear")
            third = subscriber.set_switch_state(switch_state=1, reason="all_clear")

        assert first == [BANK]
        assert second == []
        assert third == []
        # only the first decision published (and logged) the control message
        assert len(subscriber._mqtt_client.published) == 1
        published = [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control message published"
        ]
        assert len(published) == 1
        published_record: Any = published[0]
        assert published_record.reported_state == [0, 0]
        deferred = [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control retry deferred"
        ]
        assert len(deferred) == 2
        deferred_record: Any = deferred[0]
        assert deferred_record.retry_seconds == subscriber._switch_retry_seconds
        assert deferred_record.reported_state == [0, 0]

    def test_retry_after_the_window_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = self._subscriber_with_bank_off()
        subscriber.set_switch_state(switch_state=1, reason="all_clear")
        window = subscriber._switch_retry_seconds + 1
        # pretend the retry interval has elapsed before each decision
        subscriber._last_command_at[BANK] = time.time() - window
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            retried = subscriber.set_switch_state(switch_state=1, reason="all_clear")
            subscriber._last_command_at[BANK] = time.time() - window
            again = subscriber.set_switch_state(switch_state=1, reason="all_clear")

        # a retry publishes but is not an effective change to notify about
        assert retried == []
        assert again == []
        assert len(subscriber._mqtt_client.published) == 3
        retry_records = [
            r
            for r in caplog.records
            if r.getMessage() == "Retrying switch bank control message"
        ]
        assert len(retry_records) == 2
        retry_record: Any = retry_records[0]
        assert retry_record.reported_state == [0, 0]
        warnings = [
            r
            for r in caplog.records
            if r.getMessage()
            == "Switch controller did not acknowledge the commanded state"
        ]
        assert len(warnings) == 1
        record: Any = warnings[0]
        assert record.switch_bank == BANK
        assert record.commanded_state == 1
        assert record.reported_state == [0, 0]
        assert record.reason == "all_clear"
        assert record.retry_seconds == subscriber._switch_retry_seconds

    def test_reported_change_re_asserts_immediately(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = self._subscriber_with_bank_off()
        subscriber.set_switch_state(switch_state=1, reason="all_clear")
        # the bank moved to a different state while the command was pending
        subscriber.on_message(None, None, _state_message(BANK, [1, 0]))
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            changed = subscriber.set_switch_state(switch_state=1, reason="all_clear")

        assert changed == [BANK]
        assert len(subscriber._mqtt_client.published) == 2
        published = [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control message published"
        ]
        assert len(published) == 1
        published_record: Any = published[0]
        assert published_record.reported_state == [1, 0]

    def test_converged_bank_clears_the_unacknowledged_warning(self) -> None:
        subscriber = self._subscriber_with_bank_off()
        subscriber.set_switch_state(switch_state=1, reason="all_clear")
        subscriber._warn_unacknowledged_switch_bank(
            switch_bank=BANK, switch_state=1, reported_state=[0, 0], reason="all_clear"
        )
        assert subscriber._unacknowledged_banks_warned == {BANK}
        # the bank catches up: the episode is over
        subscriber.on_message(None, None, _state_message(BANK, [1, 1]))
        changed = subscriber.set_switch_state(switch_state=1, reason="all_clear")
        assert changed == []
        assert subscriber._unacknowledged_banks_warned == set()

    def test_forced_command_bypasses_the_retry_window(self) -> None:
        subscriber = self._subscriber_with_bank_off()
        subscriber.set_switch_state(switch_state=1, reason="all_clear")
        forced = subscriber.set_switch_state(
            switch_state=1, reason="manual_restore", force=True
        )
        assert forced == [BANK]
        assert len(subscriber._mqtt_client.published) == 2

    def test_manual_command_re_asserts_a_pending_command(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = _new_subscriber(banks=[BANK])
        subscriber.on_message(None, None, _state_message(BANK, [1, 1]))
        app_socket = FakeAppSocket()
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            subscriber._apply_manual_switch_command(
                app_socket=app_socket, command="start_load_shed"
            )
            subscriber._apply_manual_switch_command(
                app_socket=app_socket, command="start_load_shed"
            )

        # both manual commands publish, because the bank ignores the command
        assert len(subscriber._mqtt_client.published) == 2
        published = [
            r
            for r in caplog.records
            if r.getMessage() == "Switch bank control message published"
        ]
        assert len(published) == 2
        published_record: Any = published[1]
        assert published_record.reason == "manual_load_shed"
        event_banks = [
            payload["switch_event"]["switch_banks"] for payload in app_socket.sent
        ]
        assert event_banks == [[BANK], [BANK]]


SUPPRESSED_MESSAGE = "Switch rationing condition suppressed by configuration"


def _inverter_sample(**overrides: Any) -> dict[str, Any]:
    """A plausible inverter sample for the decision loop."""
    sample: dict[str, Any] = {
        "alert": 0,
        "total_load_power_w": 1000.0,
        "battery_soc_pct": 80.0,
        "battery_power_w": 0.0,
        "pv1_power_w": 2000.0,
        "pv2_power_w": 1500.0,
        "grid_voltage_l1_v": 230.0,
        "grid_voltage_l2_v": 230.0,
        "inverter_l1_power_w": 500.0,
        "inverter_l2_power_w": 0.0,
    }
    sample.update(overrides)
    return sample


class TestWarnOnlySwitchConditions:
    """A disabled condition is reported but never sheds the banks."""

    @staticmethod
    def _subscriber(load_shed_enabled: bool = True) -> Any:
        subscriber = _new_subscriber(banks=[BANK])
        subscriber.on_message(None, None, _state_message(BANK, [1, 1]))
        subscriber._switch_conditions = SwitchConditionConfig(
            load_shed_enabled=load_shed_enabled
        )
        return subscriber

    def test_enabled_load_shed_publishes_the_control_message(self) -> None:
        subscriber = self._subscriber()
        subscriber._process_inverter_sample(
            app_socket=FakeAppSocket(),
            inverter_data=_inverter_sample(total_load_power_w=8000.0),
        )
        assert [p["topic"] for p in subscriber._mqtt_client.published] == [
            CONTROL_TOPIC,
            "inverter/state",
        ]

    def test_warn_only_load_shed_does_not_publish_and_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subscriber = self._subscriber(load_shed_enabled=False)
        app_socket = FakeAppSocket()
        sample = _inverter_sample(total_load_power_w=8000.0)
        with caplog.at_level(logging.WARNING, logger=APP_NAME):
            subscriber._process_inverter_sample(
                app_socket=app_socket, inverter_data=sample
            )
            subscriber._process_inverter_sample(
                app_socket=app_socket, inverter_data=sample
            )

        topics = [p["topic"] for p in subscriber._mqtt_client.published]
        assert CONTROL_TOPIC not in topics
        records = [r for r in caplog.records if r.getMessage() == SUPPRESSED_MESSAGE]
        assert len(records) == 1
        record: Any = records[0]
        assert record.levelno == logging.WARNING
        assert record.condition == "load_shed"
        assert record.switch_state == 1
        assert record.load_w == 8000.0
        stats = [p["switches"] for p in app_socket.sent if "switches" in p]
        assert stats[-1]["load_shed"] == 1
        assert stats[-1]["switch_state"] == 1

    def test_warn_only_load_shed_does_not_restore_a_manual_shed(self) -> None:
        subscriber = self._subscriber(load_shed_enabled=False)
        app_socket = FakeAppSocket()
        subscriber._apply_manual_switch_command(
            app_socket=app_socket, command="start_load_shed"
        )
        assert subscriber._manual_shed is True
        assert subscriber._mqtt_client.published[0]["topic"] == CONTROL_TOPIC
        subscriber._mqtt_client.published.clear()
        subscriber._process_inverter_sample(
            app_socket=app_socket,
            inverter_data=_inverter_sample(total_load_power_w=8000.0),
        )
        # the manual shed holds: only telemetry is published, no restore
        topics = [p["topic"] for p in subscriber._mqtt_client.published]
        assert CONTROL_TOPIC not in topics
        assert topics == ["inverter/state"]
        assert subscriber._manual_shed is True

    def test_manual_commands_toggle_the_manual_shed_flag(self) -> None:
        subscriber = self._subscriber()
        app_socket = FakeAppSocket()
        subscriber._apply_manual_switch_command(
            app_socket=app_socket, command="end_load_shed"
        )
        assert subscriber._manual_shed is False
