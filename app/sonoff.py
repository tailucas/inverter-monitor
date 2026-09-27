#!/usr/bin/env python
"""Sonoff LAN-mode switch control for switch-bank load shedding.

Sonoff BasicR2 devices running the stock (Itead/eWeLink) V3+ firmware accept
control messages in local LAN mode: an HTTP POST to port 8081 carrying an
AES-128-CBC payload keyed by the device key (the eWeLink `devicekey`, also
shown as the API key in the app's DIY mode — not the eWeLink account
`apikey`).  This module implements that payload directly and addresses each
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
SONOFF_INFO_PATH = "/zeroconf/info"
# device response excerpt kept for diagnostics
SONOFF_RESPONSE_EXCERPT_CHARS = 200
# constant for the protocol's `selfApikey` envelope field (not a credential)
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
    device_key: str = field(repr=False)
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


def build_payload(device_id: str, device_key: str, params: dict) -> dict:
    """Build the encrypted LAN-mode payload for one command.

    The documented scheme: the AES-128-CBC key is the MD5 digest of the
    device key, the IV is 16 random bytes carried base64-encoded in the
    payload, and the plaintext is the compact JSON command.
    """
    plaintext = json.dumps(params, separators=(",", ":")).encode("utf-8")
    key = MD5.new(bytes(device_key, "utf-8")).digest()
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


def build_switch_payload(device_id: str, device_key: str, state: int) -> dict:
    """Build the encrypted LAN-mode payload for one switch state."""
    switch = "on" if state == STATE_ON else "off"
    return build_payload(device_id, device_key, {"switch": switch})


def _bounded_body(response: requests.Response) -> str:
    """Return a bounded excerpt of the device response for diagnostics."""
    body = response.text or ""
    return body[:SONOFF_RESPONSE_EXCERPT_CHARS]


def sonoff_error_hint(response_body: str | None) -> str | None:
    """Return an actionable hint for a rejection reported by the device.

    The device answers ``{"error": 400}`` when it cannot decrypt the payload
    with its LAN key, which in practice means the configured device key is
    stale or belongs to another device.
    """
    if not response_body:
        return None
    try:
        error = json.loads(response_body).get("error")
    except AttributeError, ValueError:
        return None
    if error in (400, 401):
        return (
            "device rejected the encrypted payload: the configured key does "
            "not match the device's LAN key. Store the device's *device key* "
            "(the eWeLink cloud `devicekey`, shown as the API key in the "
            "app's DIY mode) as Sonoff/<device_id>/devicekey — not the "
            "eWeLink account `apikey` — and restart the app."
        )
    return None


def parse_device_ids(device_id_csv: str) -> list[str]:
    """Split the configured Sonoff device id list.

    A single id works as well as a comma-separated list; blank entries are
    dropped so an unset configuration yields no devices.
    """
    return [
        device_id.strip() for device_id in device_id_csv.split(",") if device_id.strip()
    ]


def _read_credential(
    creds_obj: Creds, creds_path: str
) -> tuple[str | None, str | None]:
    """Return a trimmed credential value and the reason it is unusable."""
    try:
        raw_value = creds_obj.get_creds(creds_path)
    except Exception as e:
        return None, str(e)
    value = raw_value.strip() if raw_value else ""
    if not value:
        return None, "empty value"
    return value, None


def load_sonoff_devices(
    creds_obj: Creds, device_ids: Iterable[str]
) -> list[SonoffDeviceConfig]:
    """Resolve the configured Sonoff devices from 1Password.

    Each field lives in the `Sonoff` item under a section named after the
    device id: `Sonoff/{id}/name`, `/devicekey`, `/address` and the optional
    boolean `/shed_only` (absent means shed-only).  The key is the device's
    *device key* (the eWeLink cloud `devicekey`, which is the LAN encryption
    key) — the eWeLink account `apikey` is a different credential and is
    never read.  A device with any required field missing is skipped with a
    WARNING; the key is never logged.
    """
    devices = []
    for device_id in device_ids:
        fields: dict[str, str] = {}
        missing_field = None
        missing_error = None
        for field_name in ("name", "devicekey", "address"):
            value, error = _read_credential(
                creds_obj, f"{SONOFF_CREDS_ITEM}/{device_id}/{field_name}"
            )
            if value is None:
                missing_field = field_name
                missing_error = error
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
            device_key=fields["devicekey"],
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
        self._device_keys = {
            device.device_id: device.device_key for device in self._devices
        }
        self._request_timeout = request_timeout
        self._session = requests.Session()
        # the stock firmware expects the JSON content type on LAN-mode posts
        self._session.headers.update({"Content-Type": "application/json;charset=UTF-8"})
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
        self._verify_device_keys()
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

    def _verify_device_keys(self) -> None:
        """Check that every device accepts the configured device key.

        A read-only ``/zeroconf/info`` request is encrypted with the same key
        and payload builder as a control message, so the reply reveals
        whether the device can decrypt our payload: ``error 400/401`` means
        the credential is not the device's LAN key and is logged as an
        actionable WARNING, while any other JSON reply (some firmwares refuse
        the info probe, e.g. with error 422) proves the key works.
        Unreachable devices are logged at DEBUG (they are normal at
        start-up).
        """
        verified = 0
        rejected = []
        for device in self._devices:
            try:
                payload = build_payload(
                    device_id=device.device_id,
                    device_key=device.device_key,
                    params={},
                )
                response = self._session.post(
                    url=(
                        f"http://{device.address}:{SONOFF_LAN_PORT}{SONOFF_INFO_PATH}"
                    ),
                    data=json.dumps(payload, separators=(",", ":")),
                    timeout=self._request_timeout,
                )
                response_body = _bounded_body(response)
                error = response.json().get("error")
            except Exception:
                log.debug(
                    "Sonoff device key check could not reach the device",
                    extra={
                        "device_id": device.device_id,
                        "address": device.address,
                    },
                    exc_info=True,
                )
                continue
            hint = sonoff_error_hint(response_body)
            if hint is not None:
                rejected.append(device.device_id)
                log.warning(
                    "Sonoff API key rejected by device",
                    extra={
                        "device_id": device.device_id,
                        "device_name": device.name,
                        "address": device.address,
                        "error": str(error),
                        "response_body": response_body,
                        "error_hint": hint,
                    },
                )
                continue
            # the device decrypted our payload; a non-zero error only means
            # this firmware does not answer the read-only info probe
            verified += 1
            log.debug(
                "Sonoff API key accepted",
                extra={
                    "device_id": device.device_id,
                    "device_name": device.name,
                    "probe_error": error,
                    "response_body": response_body,
                },
            )
        log.info(
            "Sonoff device keys verified",
            extra={
                "device_count": len(self._devices),
                "verified_count": verified,
                "rejected_count": len(rejected),
                "rejected_devices": rejected,
            },
        )

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
        response_body = None
        try:
            payload = build_switch_payload(
                device_id=command.device_id,
                device_key=self._device_keys.get(command.device_id, ""),
                state=command.state,
            )
            response = self._session.post(
                url=url,
                data=json.dumps(payload, separators=(",", ":")),
                timeout=self._request_timeout,
            )
            response_body = _bounded_body(response)
            response.raise_for_status()
            error = response.json().get("error")
            if error != 0:
                raise ValueError(f"device reported error {error!r}")
        except Exception as e:
            self._record_failure(
                command=command,
                reason=reason,
                error=e,
                response_body=response_body,
                error_hint=sonoff_error_hint(response_body),
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
        response_body: str | None = None,
        error_hint: str | None = None,
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
        if response_body is not None:
            fields["response_body"] = response_body
        if error_hint is not None:
            fields["error_hint"] = error_hint
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
