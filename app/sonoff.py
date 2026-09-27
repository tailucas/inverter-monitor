#!/usr/bin/env python
"""Sonoff LAN-mode switch control for switch-bank load shedding.

Sonoff BasicR2 devices running the stock (Itead/eWeLink) V3+ firmware accept
control messages in local LAN mode: an HTTP POST to port 8081 carrying an
AES-128-CBC payload keyed by the device API key (the documented LAN-mode
protocol).  This module implements that payload directly and addresses each
device by the IP address held in 1Password, so no mDNS discovery, no cloud
round-trip and no asyncio event loop is involved.

The decision logic (`parse_shed_only`, `plan_sonoff_commands`) is pure and
unit-tested in ``tests/test_sonoff.py``.  ``SonoffController`` owns the I/O
and runs as an ``AppThread`` so a slow or unreachable switch can never block
the inverter decision loop.
"""

import base64
import json
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import requests
from Crypto.Cipher import AES
from Crypto.Hash import MD5
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad
from requests.exceptions import RequestException
from tailucas_pylib import log, threads
from tailucas_pylib.app import AppThread
from tailucas_pylib.creds import Creds

# 1Password item holding one section per configured Sonoff device id
SONOFF_CREDS_ITEM = "Sonoff"
# LAN-mode control endpoint exposed by the stock firmware
SONOFF_LAN_PORT = 8081
SONOFF_SWITCH_PATH = "/zeroconf/switch"
# constant the LAN-mode protocol expects alongside the device API key
SONOFF_SELF_API_KEY = "123"
SONOFF_REQUEST_TIMEOUT_SECONDS = 3
# per-device retry backoff after a failed control message (doubles to the cap)
SONOFF_RETRY_BACKOFF_SECONDS = 5
SONOFF_MAX_RETRY_BACKOFF_SECONDS = 60
# switch states as commanded by a switch-bank decision
STATE_OFF = 0
STATE_ON = 1
# values accepted for the optional `shed_only` credential
_FALSE_VALUES = {"false", "0", "no", "n", "off"}


@dataclass(frozen=True)
class SonoffDeviceConfig:
    """A Sonoff device resolved from configuration and 1Password."""

    device_id: str
    name: str
    address: str
    api_key: str = field(repr=False)
    shed_only: bool = True


@dataclass(frozen=True)
class SonoffCommand:
    """A control message to issue for one switch-bank decision."""

    device_id: str
    name: str
    address: str
    state: int


def parse_shed_only(value: str | None) -> bool:
    """Parse the optional `shed_only` credential.

    A shed-only device is switched off when load shedding is needed but is
    never switched back on by the application.  Absent, blank or unrecognised
    values keep the default (True), which means only ever switch off.
    """
    if value is None:
        return True
    if value.strip().lower() in _FALSE_VALUES:
        return False
    return True


def plan_sonoff_commands(
    devices: Sequence[SonoffDeviceConfig],
    switch_state: int,
    commanded_states: Mapping[str, int],
) -> list[SonoffCommand]:
    """Decide which control messages one switch-bank decision requires.

    `commanded_states` maps a device id to the last successfully commanded
    state, so a repeated decision does not re-issue control messages.  Failed
    commands are not recorded by the controller, which makes the next
    decision retry them.
    """
    commands = []
    for device in devices:
        if switch_state == STATE_OFF:
            desired_state = STATE_OFF
        elif device.shed_only:
            # a restore leaves a shed-only device exactly as it is
            continue
        else:
            desired_state = STATE_ON
        if commanded_states.get(device.device_id) == desired_state:
            continue
        commands.append(
            SonoffCommand(
                device_id=device.device_id,
                name=device.name,
                address=device.address,
                state=desired_state,
            )
        )
    return commands


def build_switch_payload(device_id: str, api_key: str, state: int) -> dict:
    """Build the encrypted LAN-mode payload for one switch state.

    The documented scheme: the AES-128-CBC key is the MD5 digest of the
    device API key, the IV is 16 random bytes carried base64-encoded in the
    payload, and the plaintext is the compact JSON switch command.
    """
    switch = "on" if state == STATE_ON else "off"
    plaintext = json.dumps({"switch": switch}, separators=(",", ":")).encode("utf-8")
    key = MD5.new(bytes(api_key, "utf-8")).digest()
    iv = get_random_bytes(AES.block_size)
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
    return {
        "sequence": str(int(time.time() * 1000)),
        "deviceid": device_id,
        "selfApikey": SONOFF_SELF_API_KEY,
        "iv": base64.b64encode(iv).decode("utf-8"),
        "encrypt": True,
        "data": base64.b64encode(ciphertext).decode("utf-8"),
    }


def parse_device_ids(device_id_csv: str) -> list[str]:
    """Split the configured Sonoff device id list.

    A single id works as well as a comma-separated list; blank entries are
    dropped so an unset configuration yields no devices.
    """
    return [
        device_id.strip() for device_id in device_id_csv.split(",") if device_id.strip()
    ]


def load_sonoff_devices(
    creds_obj: Creds, device_ids: Iterable[str]
) -> list[SonoffDeviceConfig]:
    """Resolve the configured Sonoff devices from 1Password.

    Each field lives in the `Sonoff` item under a section named after the
    device id: `Sonoff/{id}/name`, `/apikey`, `/address` and the optional
    boolean `/shed_only` (absent means shed-only).  A device with any
    required field missing is skipped with a WARNING; the API key is never
    logged.
    """
    devices = []
    for device_id in device_ids:
        fields: dict[str, str] = {}
        missing_field = None
        missing_error = None
        for field_name in ("name", "apikey", "address"):
            creds_path = f"{SONOFF_CREDS_ITEM}/{device_id}/{field_name}"
            try:
                raw_value = creds_obj.get_creds(creds_path)
                value = raw_value.strip() if raw_value else ""
            except Exception as e:
                missing_field = field_name
                missing_error = str(e)
                break
            if not value:
                missing_field = field_name
                missing_error = "empty value"
                break
            fields[field_name] = value
        if missing_field is not None:
            log.warning(
                "Skipping Sonoff device with incomplete credentials",
                extra={
                    "device_id": device_id,
                    "field": missing_field,
                    "error": missing_error,
                },
            )
            continue
        try:
            shed_only_field: str | None = creds_obj.get_creds(
                f"{SONOFF_CREDS_ITEM}/{device_id}/shed_only"
            )
        except Exception:
            shed_only_field = None
        device = SonoffDeviceConfig(
            device_id=device_id,
            name=fields["name"],
            address=fields["address"],
            api_key=fields["apikey"],
            shed_only=parse_shed_only(shed_only_field),
        )
        log.info(
            "Sonoff device configured",
            extra={
                "device_id": device.device_id,
                "device_name": device.name,
                "address": device.address,
                "shed_only": device.shed_only,
            },
        )
        devices.append(device)
    return devices


class SonoffController(AppThread):
    """Issue Sonoff control messages for switch-bank decisions.

    Load-shed decisions are handed over from the MQTT subscriber thread with
    `apply`; this thread owns the HTTP session, the last successfully
    commanded state per device and the per-device retry backoff, so a slow or
    unreachable device can never block the inverter decision loop.
    """

    def __init__(
        self,
        devices: Sequence[SonoffDeviceConfig],
        request_timeout: float = SONOFF_REQUEST_TIMEOUT_SECONDS,
    ):
        AppThread.__init__(self, name=self.__class__.__name__)
        self._devices = list(devices)
        self._api_keys = {device.device_id: device.api_key for device in self._devices}
        self._request_timeout = request_timeout
        self._session = requests.Session()
        self._wake = threading.Event()
        self._intent_lock = threading.Lock()
        self._intent: tuple[int, str] | None = None
        self._last_commanded: dict[str, int] = {}
        self._failed_state: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._backoff_alerted: set[str] = set()

    def apply(self, switch_state: int, reason: str) -> None:
        """Queue a switch-bank decision for the controller thread."""
        with self._intent_lock:
            self._intent = (int(switch_state), str(reason))
        self._wake.set()

    def run(self):
        log.info(
            "Sonoff controller started",
            extra={
                "device_count": len(self._devices),
                "shed_only_count": sum(
                    1 for device in self._devices if device.shed_only
                ),
            },
        )
        # the wake event keeps the wait interruptible with a bounded
        # shutdown latency, so no terminator thread is needed
        while not threads.shutting_down:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            with self._intent_lock:
                intent = self._intent
                self._intent = None
            if intent is None:
                continue
            try:
                self._issue(intent=intent)
            except Exception:
                # a decision must never take the controller thread down
                log.warning(
                    "Unexpected error issuing Sonoff control messages.",
                    exc_info=True,
                )
        try:
            self._session.close()
        except Exception:
            log.warning("Ignoring error closing Sonoff HTTP session.", exc_info=True)
        log.info("Sonoff controller stopped.")

    def _issue(self, intent: tuple[int, str]) -> None:
        switch_state, reason = intent
        now = time.time()
        commands = plan_sonoff_commands(
            devices=self._devices,
            switch_state=switch_state,
            commanded_states=self._last_commanded,
        )
        for command in commands:
            retry_at = self._retry_at.get(command.device_id)
            if (
                retry_at is not None
                and now < retry_at
                and self._failed_state.get(command.device_id) == command.state
            ):
                # only a retry of the same failed command waits out the
                # backoff; a new decision (including a manual one) is
                # attempted immediately
                log.debug(
                    "Sonoff control message deferred by retry backoff",
                    extra={
                        "device_id": command.device_id,
                        "device_name": command.name,
                        "switch_state": command.state,
                        "reason": reason,
                        "retry_in_seconds": round(retry_at - now, 1),
                    },
                )
                continue
            self._send(command=command, reason=reason)

    def _send(self, command: SonoffCommand, reason: str) -> None:
        url = f"http://{command.address}:{SONOFF_LAN_PORT}{SONOFF_SWITCH_PATH}"
        try:
            payload = build_switch_payload(
                device_id=command.device_id,
                api_key=self._api_keys.get(command.device_id, ""),
                state=command.state,
            )
            response = self._session.post(
                url=url,
                data=json.dumps(payload, separators=(",", ":")),
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            error = response.json().get("error")
            if error != 0:
                raise ValueError(f"device reported error {error!r}")
        except Exception as e:
            self._record_failure(
                command=command,
                reason=reason,
                error=e,
                exc_info=not isinstance(e, RequestException),
            )
            return
        self._record_success(command=command, reason=reason)

    def _record_success(self, command: SonoffCommand, reason: str) -> None:
        self._last_commanded[command.device_id] = command.state
        self._failed_state.pop(command.device_id, None)
        self._failures.pop(command.device_id, None)
        self._retry_at.pop(command.device_id, None)
        self._backoff_alerted.discard(command.device_id)
        log.info(
            "Sonoff control message issued",
            extra={
                "device_id": command.device_id,
                "device_name": command.name,
                "address": command.address,
                "switch_state": command.state,
                "reason": reason,
            },
        )

    def _record_failure(
        self,
        command: SonoffCommand,
        reason: str,
        error: Exception,
        exc_info: bool = False,
    ) -> None:
        failures = self._failures.get(command.device_id, 0) + 1
        self._failures[command.device_id] = failures
        self._failed_state[command.device_id] = command.state
        backoff = min(
            SONOFF_RETRY_BACKOFF_SECONDS * (2 ** (failures - 1)),
            SONOFF_MAX_RETRY_BACKOFF_SECONDS,
        )
        self._retry_at[command.device_id] = time.time() + backoff
        fields = {
            "device_id": command.device_id,
            "device_name": command.name,
            "address": command.address,
            "switch_state": command.state,
            "reason": reason,
            "failures": failures,
            "retry_in_seconds": backoff,
            "error": str(error),
            "error_type": type(error).__name__,
        }
        if command.device_id in self._backoff_alerted:
            log.debug("Sonoff control message retry failed", extra=fields)
        elif backoff >= SONOFF_MAX_RETRY_BACKOFF_SECONDS:
            self._backoff_alerted.add(command.device_id)
            log.error(
                "Sonoff control message retry backoff reached the maximum",
                extra=fields,
            )
        else:
            log.warning(
                "Sonoff control message failed", extra=fields, exc_info=exc_info
            )
