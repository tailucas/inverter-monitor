#!/usr/bin/env python
"""Unit tests for the Sonoff LAN-mode switch control.

Covers the pure decision logic (shed/restore semantics and change gating),
the configuration parsing, the 1Password device resolution and the
controller send path with the HTTP transport mocked out.
"""

import base64
import json
import logging
import time
from hashlib import md5
from typing import Any

import pytest
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from tailucas_pylib import APP_NAME

import app.sonoff as sonoff_module
from app.sonoff import (
    SONOFF_INFO_PATH,
    SONOFF_LAN_PORT,
    SONOFF_REQUEST_TIMEOUT_SECONDS,
    SONOFF_SWITCH_PATH,
    STATE_OFF,
    STATE_ON,
    SonoffController,
    SonoffDeviceConfig,
    build_switch_payload,
    load_sonoff_devices,
    parse_device_ids,
    parse_shed_only,
    plan_sonoff_commands,
)

# the device key (LAN key) is the credential used for LAN-mode encryption
DEVICE_KEY = "12345678-1234-1234-1234-123456789abc"
# deterministic test vector pinning the AES-128-CBC LAN-mode payload
FIXED_IV = bytes(range(16))
FIXED_IV_B64 = "AAECAwQFBgcICQoLDA0ODw=="
ON_CIPHERTEXT_B64 = "zon/LJ2YepKioBOU7nG74A=="
OFF_CIPHERTEXT_B64 = "mny47kv/uKUfIbMWe9uFdYshSm2Ww7OezwfNSk7bJms="


def _device(device_id: str, shed_only: bool = True) -> SonoffDeviceConfig:
    return SonoffDeviceConfig(
        device_id=device_id,
        name=f"name-{device_id}",
        address="192.168.1.10",
        device_key=DEVICE_KEY,
        shed_only=shed_only,
    )


class FakeCreds:
    """Creds stand-in returning the configured fields per 1Password path."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields

    def get_creds(self, path: str) -> str:
        if path not in self.fields:
            raise AssertionError(f"no credential for {path}")
        return self.fields[path]


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> Any:
        return self._payload


class TestParseDeviceIds:
    """The `[sonoff] device_id_csv` entry splits like the other CSVs."""

    def test_single_element(self) -> None:
        assert parse_device_ids("shed1") == ["shed1"]

    def test_multiple_elements_with_whitespace(self) -> None:
        assert parse_device_ids(" shed1 , shed2 ,shed3 ") == [
            "shed1",
            "shed2",
            "shed3",
        ]

    def test_blank_entries_are_dropped(self) -> None:
        assert parse_device_ids("") == []
        assert parse_device_ids(" , ") == []
        assert parse_device_ids("shed1,") == ["shed1"]


class TestParseShedOnly:
    """`shed_only` defaults to True (only ever switch off)."""

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", " ON "])
    def test_true_values(self, value: str) -> None:
        assert parse_shed_only(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", " OFF "])
    def test_false_values(self, value: str) -> None:
        assert parse_shed_only(value) is False

    @pytest.mark.parametrize("value", [None, "", "   ", "unexpected"])
    def test_defaults_to_true(self, value: str | None) -> None:
        assert parse_shed_only(value) is True


class TestPlanSonoffCommands:
    """Switch-bank decisions map to per-device Sonoff commands."""

    def test_shed_commands_every_device(self) -> None:
        devices = [
            _device("shed1", shed_only=True),
            _device("shed2", shed_only=False),
        ]
        commands = plan_sonoff_commands(devices, STATE_OFF, {})
        assert [(c.device_id, c.state) for c in commands] == [
            ("shed1", STATE_OFF),
            ("shed2", STATE_OFF),
        ]

    def test_restore_only_switches_devices_that_may_be_restored(self) -> None:
        devices = [
            _device("shed1", shed_only=True),
            _device("shed2", shed_only=False),
        ]
        commands = plan_sonoff_commands(devices, STATE_ON, {})
        assert [(c.device_id, c.state) for c in commands] == [("shed2", STATE_ON)]

    def test_repeated_decision_is_not_re_issued(self) -> None:
        devices = [_device("shed1", shed_only=False)]
        assert plan_sonoff_commands(devices, STATE_ON, {"shed1": STATE_ON}) == []
        commands = plan_sonoff_commands(devices, STATE_OFF, {"shed1": STATE_ON})
        assert [(c.device_id, c.state) for c in commands] == [("shed1", STATE_OFF)]

    def test_shed_only_device_already_off_yields_no_command(self) -> None:
        devices = [_device("shed1", shed_only=True)]
        assert plan_sonoff_commands(devices, STATE_OFF, {"shed1": STATE_OFF}) == []

    def test_unknown_command_state_issues_command(self) -> None:
        devices = [_device("shed1", shed_only=True)]
        assert plan_sonoff_commands(devices, STATE_OFF, {})
        assert plan_sonoff_commands(devices, STATE_ON, {}) == []


class TestBuildSwitchPayload:
    """The LAN-mode payload carries the documented AES-CBC ciphertext."""

    def test_protocol_test_vector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sonoff_module, "get_random_bytes", lambda _size: FIXED_IV)
        on_payload = build_switch_payload("shed1", DEVICE_KEY, STATE_ON)
        assert on_payload["iv"] == FIXED_IV_B64
        assert on_payload["data"] == ON_CIPHERTEXT_B64
        assert on_payload["deviceid"] == "shed1"
        assert on_payload["selfApikey"] == "123"
        assert on_payload["encrypt"] is True
        assert str(on_payload["sequence"]).isdigit()
        off_payload = build_switch_payload("shed1", DEVICE_KEY, STATE_OFF)
        assert off_payload["data"] == OFF_CIPHERTEXT_B64

    def test_round_trip_is_compact_json_switch_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sonoff_module, "get_random_bytes", lambda _size: FIXED_IV)
        payload = build_switch_payload("shed1", DEVICE_KEY, STATE_ON)
        key = md5(DEVICE_KEY.encode()).digest()
        cipher = AES.new(key, AES.MODE_CBC, iv=base64.b64decode(payload["iv"]))
        plaintext = unpad(
            cipher.decrypt(base64.b64decode(payload["data"])), AES.block_size
        )
        assert plaintext == b'{"switch":"on"}'
        # the payload survives JSON serialisation for the HTTP body
        assert json.loads(json.dumps(payload))["encrypt"] is True


def _creds_fields(device_id: str, **overrides: str) -> dict[str, str]:
    fields = {
        f"Sonoff/{device_id}/name": f"name-{device_id}",
        f"Sonoff/{device_id}/devicekey": DEVICE_KEY,
        f"Sonoff/{device_id}/address": "192.168.1.50",
    }
    fields.update(overrides)
    return fields


class TestLoadSonoffDevices:
    """Devices resolve from 1Password sections named after the id."""

    def test_resolves_fields_and_logs_startup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fields = _creds_fields("shed1", **{"Sonoff/shed1/shed_only": "false"})
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            devices = load_sonoff_devices(FakeCreds(fields), ["shed1"])
        assert len(devices) == 1
        device = devices[0]
        assert device.device_id == "shed1"
        assert device.name == "name-shed1"
        assert device.address == "192.168.1.50"
        assert device.device_key == DEVICE_KEY
        assert device.shed_only is False
        configured = [
            r for r in caplog.records if r.getMessage() == "Sonoff device configured"
        ]
        assert len(configured) == 1
        record: Any = configured[0]
        assert record.device_id == "shed1"
        assert record.device_name == "name-shed1"
        assert record.address == "192.168.1.50"
        # the device key must never end up in a log record
        assert DEVICE_KEY not in str(record.__dict__)

    def test_shed_only_defaults_to_true(self) -> None:
        devices = load_sonoff_devices(FakeCreds(_creds_fields("shed1")), ["shed1"])
        assert devices[0].shed_only is True

    def test_devicekey_credential_is_required(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An account apikey is not accepted in place of the device key."""
        fields = _creds_fields("shed1")
        del fields["Sonoff/shed1/devicekey"]
        fields["Sonoff/shed1/apikey"] = "731e4609-08da-40df-a173-7404c6e5a7f6"
        with caplog.at_level(logging.WARNING, logger=APP_NAME):
            devices = load_sonoff_devices(FakeCreds(fields), ["shed1"])
        assert devices == []
        skipped = [
            r
            for r in caplog.records
            if r.getMessage() == "Skipping Sonoff device with incomplete credentials"
        ]
        assert len(skipped) == 1
        record: Any = skipped[0]
        assert record.device_id == "shed1"
        assert record.field == "devicekey"

    def test_incomplete_credentials_are_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fields = _creds_fields("shed1")
        del fields["Sonoff/shed1/address"]
        with caplog.at_level(logging.WARNING, logger=APP_NAME):
            devices = load_sonoff_devices(FakeCreds(fields), ["shed1"])
        assert devices == []
        skipped = [
            r
            for r in caplog.records
            if r.getMessage() == "Skipping Sonoff device with incomplete credentials"
        ]
        assert len(skipped) == 1
        record: Any = skipped[0]
        assert record.device_id == "shed1"
        assert record.field == "address"

    def test_blank_credential_is_skipped(self) -> None:
        fields = _creds_fields("shed1")
        fields["Sonoff/shed1/name"] = "   "
        assert load_sonoff_devices(FakeCreds(fields), ["shed1"]) == []


class TestSonoffController:
    """The controller issues control messages and backs off on failure."""

    def test_session_sends_the_json_content_type(self) -> None:
        """The LAN-mode POST carries the content type the firmware expects."""
        controller = SonoffController(devices=[_device("shed1")])
        assert (
            controller._session.headers["Content-Type"]
            == "application/json;charset=UTF-8"
        )

    @staticmethod
    def _post_recorder(posts: list[dict[str, Any]], response: Any) -> Any:
        def fake_post(self, url, data=None, timeout=None, **kwargs):
            posts.append({"url": url, "data": data, "timeout": timeout})
            return response

        return fake_post

    def test_issues_and_records_control_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        posts: list[dict[str, Any]] = []
        monkeypatch.setattr(
            requests.Session,
            "post",
            self._post_recorder(posts, FakeResponse({"error": 0})),
        )
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            controller._issue(intent=(STATE_ON, "all_clear"))
        expected_url = f"http://192.168.1.10:{SONOFF_LAN_PORT}{SONOFF_SWITCH_PATH}"
        assert posts[0]["url"] == expected_url
        assert posts[0]["timeout"] == SONOFF_REQUEST_TIMEOUT_SECONDS
        payload = json.loads(posts[0]["data"])
        assert payload["deviceid"] == "shed1"
        assert controller._last_commanded == {"shed1": STATE_ON}
        issued = [
            r
            for r in caplog.records
            if r.getMessage() == "Sonoff control message issued"
        ]
        assert len(issued) == 1
        record: Any = issued[0]
        assert record.device_id == "shed1"
        assert record.switch_state == STATE_ON
        assert record.reason == "all_clear"
        assert record.response_status == 200
        assert record.response_body == '{"error": 0}'

    def test_shed_only_device_is_not_restored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posts: list[dict[str, Any]] = []
        monkeypatch.setattr(
            requests.Session,
            "post",
            self._post_recorder(posts, FakeResponse({"error": 0})),
        )
        controller = SonoffController(devices=[_device("shed1", shed_only=True)])
        controller._issue(intent=(STATE_ON, "all_clear"))
        assert posts == []
        assert controller._last_commanded == {}

    def test_failure_warns_and_backs_off(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        posts: list[dict[str, Any]] = []

        def failing_post(self, url, data=None, timeout=None, **kwargs):
            posts.append({"url": url})
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(requests.Session, "post", failing_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            controller._issue(intent=(STATE_ON, "all_clear"))
            assert "shed1" not in controller._last_commanded
            assert controller._failures == {"shed1": 1}
            assert controller._retry_at["shed1"] > time.time()
            # the next decision re-plans the command but defers the retry
            controller._issue(intent=(STATE_ON, "all_clear"))
        assert len(posts) == 1
        failed = [
            r
            for r in caplog.records
            if r.getMessage() == "Sonoff control message failed"
        ]
        assert len(failed) == 1
        record: Any = failed[0]
        assert record.device_id == "shed1"
        assert record.error_type == "ConnectionError"
        assert record.retry_in_seconds == 5
        deferred = [
            r
            for r in caplog.records
            if r.getMessage() == "Sonoff control message deferred by retry backoff"
        ]
        assert len(deferred) == 1

    def test_backoff_escalates_to_one_error_at_the_maximum(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def failing_post(self, url, data=None, timeout=None, **kwargs):
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(requests.Session, "post", failing_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        controller._failures["shed1"] = 4  # the next failure reaches the cap
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            controller._issue(intent=(STATE_ON, "all_clear"))
            assert controller._retry_at["shed1"] - time.time() > 50
            controller._retry_at["shed1"] = 0.0
            controller._issue(intent=(STATE_ON, "all_clear"))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].getMessage() == (
            "Sonoff control message retry backoff reached the maximum"
        )
        assert controller._backoff_alerted == {"shed1"}

    def test_success_clears_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def ok_post(self, url, data=None, timeout=None, **kwargs):
            return FakeResponse({"error": 0})

        monkeypatch.setattr(requests.Session, "post", ok_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        controller._failures["shed1"] = 2
        controller._retry_at["shed1"] = 0.0
        controller._failed_state["shed1"] = STATE_ON
        controller._backoff_alerted.add("shed1")
        controller._issue(intent=(STATE_OFF, "load_shed"))
        assert controller._failures == {}
        assert controller._retry_at == {}
        assert controller._failed_state == {}
        assert controller._backoff_alerted == set()
        assert controller._last_commanded == {"shed1": STATE_OFF}

    def test_new_state_is_not_deferred_by_a_previous_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh decision is attempted even while a device backs off."""
        calls: list[int] = []

        def flaky_post(self, url, data=None, timeout=None, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("no route to host")
            return FakeResponse({"error": 0})

        monkeypatch.setattr(requests.Session, "post", flaky_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        controller._issue(intent=(STATE_ON, "all_clear"))
        assert controller._failures == {"shed1": 1}
        # the opposite state must not wait out the retry backoff
        controller._issue(intent=(STATE_OFF, "load_shed"))
        assert len(calls) == 2
        assert controller._last_commanded == {"shed1": STATE_OFF}
        assert controller._failures == {}

    def test_device_error_response_is_treated_as_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def error_post(self, url, data=None, timeout=None, **kwargs):
            return FakeResponse({"error": 401})

        monkeypatch.setattr(requests.Session, "post", error_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        controller._issue(intent=(STATE_ON, "all_clear"))
        assert controller._last_commanded == {}
        assert controller._failures == {"shed1": 1}

    def test_malformed_response_is_treated_as_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def bad_post(self, url, data=None, timeout=None, **kwargs):
            return FakeResponse(["not", "an", "object"])

        monkeypatch.setattr(requests.Session, "post", bad_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        controller._issue(intent=(STATE_ON, "all_clear"))
        assert controller._failures == {"shed1": 1}

    def test_device_rejection_reports_body_and_hint(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A device-side rejection carries its body and an actionable hint."""

        def rejected_post(self, url, data=None, timeout=None, **kwargs):
            return FakeResponse({"seq": 3, "sequence": "1", "error": 400})

        monkeypatch.setattr(requests.Session, "post", rejected_post)
        controller = SonoffController(devices=[_device("shed1", shed_only=False)])
        with caplog.at_level(logging.WARNING, logger=APP_NAME):
            controller._issue(intent=(STATE_OFF, "load_shed"))
        failed = [
            r
            for r in caplog.records
            if r.getMessage() == "Sonoff control message failed"
        ]
        record: Any = failed[0]
        assert '"error": 400' in record.response_body
        assert "does not match the device's LAN key" in record.error_hint

    def test_startup_key_check_warns_on_rejected_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A key the device refuses is reported once at start-up."""
        posts: list[str] = []

        def rejected_post(self, url, data=None, timeout=None, **kwargs):
            posts.append(url)
            return FakeResponse({"seq": 3, "sequence": "1", "error": 401})

        monkeypatch.setattr(requests.Session, "post", rejected_post)
        controller = SonoffController(devices=[_device("shed1")])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            controller._verify_device_keys()
        assert posts == [f"http://192.168.1.10:{SONOFF_LAN_PORT}{SONOFF_INFO_PATH}"]
        rejected = [
            r
            for r in caplog.records
            if r.getMessage() == "Sonoff API key rejected by device"
        ]
        assert len(rejected) == 1
        record: Any = rejected[0]
        assert record.device_id == "shed1"
        assert "does not match the device's LAN key" in record.error_hint
        summary = [
            r for r in caplog.records if r.getMessage() == "Sonoff device keys verified"
        ]
        summary_record: Any = summary[0]
        assert summary_record.verified_count == 0
        assert summary_record.rejected_count == 1
        assert summary_record.rejected_devices == ["shed1"]

    def test_startup_key_check_accepts_valid_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A device that accepts the payload counts as verified."""

        def ok_post(self, url, data=None, timeout=None, **kwargs):
            return FakeResponse({"seq": 3, "sequence": "1", "error": 0})

        monkeypatch.setattr(requests.Session, "post", ok_post)
        controller = SonoffController(devices=[_device("shed1")])
        with caplog.at_level(logging.INFO, logger=APP_NAME):
            controller._verify_device_keys()
        summary = [
            r for r in caplog.records if r.getMessage() == "Sonoff device keys verified"
        ]
        summary_record: Any = summary[0]
        assert summary_record.device_count == 1
        assert summary_record.verified_count == 1
        assert summary_record.rejected_count == 0

    def test_startup_key_check_counts_refused_probe_as_verified(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refused info probe still proves the device decrypted our key."""
        posts: list[str] = []

        def refused_post(self, url, data=None, timeout=None, **kwargs):
            posts.append(url)
            return FakeResponse({"seq": 4, "sequence": "1", "error": 422})

        monkeypatch.setattr(requests.Session, "post", refused_post)
        controller = SonoffController(devices=[_device("shed1")])
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            controller._verify_device_keys()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        accepted = [
            r for r in caplog.records if r.getMessage() == "Sonoff API key accepted"
        ]
        assert len(accepted) == 1
        accepted_record: Any = accepted[0]
        assert accepted_record.probe_error == 422
        summary = [
            r for r in caplog.records if r.getMessage() == "Sonoff device keys verified"
        ]
        summary_record: Any = summary[0]
        assert summary_record.verified_count == 1
        assert summary_record.rejected_count == 0

    def test_startup_key_check_ignores_unreachable_device(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An offline device at start-up is not reported as a key failure."""

        def unreachable_post(self, url, data=None, timeout=None, **kwargs):
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(requests.Session, "post", unreachable_post)
        controller = SonoffController(devices=[_device("shed1")])
        with caplog.at_level(logging.DEBUG, logger=APP_NAME):
            controller._verify_device_keys()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        summary = [
            r for r in caplog.records if r.getMessage() == "Sonoff device keys verified"
        ]
        summary_record: Any = summary[0]
        assert summary_record.device_count == 1
        assert summary_record.verified_count == 0
        assert summary_record.rejected_count == 0
