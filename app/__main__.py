#!/usr/bin/env python
import binascii
import os
import re
import socket
import threading
import time
from collections import deque
from pathlib import Path

import libscrc
import paho.mqtt.client as mqtt
import requests
import simplejson as json
import zmq
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind
from pagerduty import EventsApiV2Client
from paho.mqtt.client import MQTT_ERR_NO_CONN
from requests.adapters import ConnectionError
from requests.exceptions import RequestException
from sentry_sdk.integrations.logging import ignore_logger
from simplejson import JSONDecodeError
from tailucas_pylib import APP_NAME, DEVICE_NAME_BASE, app_config, log, threads, tracing
from tailucas_pylib.app import AppThread
from tailucas_pylib.creds import Creds
from tailucas_pylib.flags import is_flag_enabled
from tailucas_pylib.handler import exception_handler
from tailucas_pylib.process import SignalHandler
from tailucas_pylib.threads import bye, die, thread_nanny
from tailucas_pylib.zmq import URL_WORKER_APP, Closable, zmq_term
from zmq.error import ContextTerminated, ZMQError

from app.metrics import configure as metrics_configure
from app.serial_reader import SerialPortReader
from app.telegram_bot import URL_WORKER_TELEGRAM  # noqa: E402

creds: Creds | None = None
debug_metrics = app_config.get("metrics", "debug_csv").split(",")

URL_WORKER_MQTT_PUBLISH = "inproc://mqtt-publish"

DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
ERROR_RETRY_INTERVAL_SECONDS = 5
IMPLAUSIBLE_CHANGE_PERCENTAGE = 5
BATTERY_LOW_PCT = 45
# assuming CFE drop-out at 30%
BATTERY_CRITICAL_PCT = 40
# idle small home ~ 300W
BATTERY_MAJOR_DRAW_W = 500
# BMS serial data loss timeout
BMS_DATA_LOSS_TIMEOUT = 600

# OpenTelemetry meter and tracer (module-level, shared across all threads)
OTEL_METER = metrics.get_meter(APP_NAME)
OTEL_TRACER = trace.get_tracer(APP_NAME)


def format_traceparent(span: trace.Span) -> str:
    """Build a W3C traceparent string from the current span's context."""
    ctx = span.get_span_context()
    return (
        f"00-{trace.format_trace_id(ctx.trace_id)}"
        f"-{trace.format_span_id(ctx.span_id)}-{ctx.trace_flags:02x}"
    )


def twos_complement_hex(hexval):
    bits = 16
    val = int(hexval, bits)
    if val & (1 << (bits - 1)):
        val -= 1 << bits
    return val


class LoggerReader(AppThread):
    def __init__(
        self,
        field_mappings,
        logger_sn,
        logger_ip,
        logger_port,
        sample_interval_secs=DEFAULT_SAMPLE_INTERVAL_SECONDS,
    ):
        AppThread.__init__(self, name=self.__class__.__name__)
        self.field_mappings = field_mappings
        self.logger_sn = logger_sn
        self.logger_ip = logger_ip
        self.logger_port = logger_port
        self.sample_interval_secs = sample_interval_secs

    def get_logger_data(self):
        output = {}

        client_socket: socket.socket | None = None
        pini = 59
        pfin = 112
        chunks = 0
        while chunks < 2:
            start = binascii.unhexlify("A5")  # start
            length = binascii.unhexlify("1700")  # datalength
            controlcode = binascii.unhexlify("1045")  # controlCode
            serial = binascii.unhexlify("0000")  # serial
            datafield = binascii.unhexlify(
                "020000000000000000000000000000"
            )  # com.igen.localmode.dy.instruction.send.SendDataField
            pos_ini = str(hex(pini)[2:4].zfill(4))
            pos_fin = str(hex(pfin - pini + 1)[2:4].zfill(4))
            businessfield = binascii.unhexlify(
                "0103" + pos_ini + pos_fin
            )  # sin CRC16MODBUS
            crc = binascii.unhexlify(
                str(hex(libscrc.modbus(businessfield))[4:6])
                + str(hex(libscrc.modbus(businessfield))[2:4])
            )  # CRC16modbus
            checksum_placeholder = binascii.unhexlify("00")  # checksum F2
            endCode = binascii.unhexlify("15")

            inverter_sn2 = bytearray.fromhex(
                hex(self.logger_sn)[8:10]
                + hex(self.logger_sn)[6:8]
                + hex(self.logger_sn)[4:6]
                + hex(self.logger_sn)[2:4]
            )
            frame = bytearray(
                start
                + length
                + controlcode
                + serial
                + inverter_sn2
                + datafield
                + businessfield
                + crc
                + checksum_placeholder
                + endCode
            )

            checksum: int = 0
            frame_bytes = bytearray(frame)
            for i in range(1, len(frame_bytes) - 2):
                checksum += frame_bytes[i] & 255
            frame_bytes[len(frame_bytes) - 2] = checksum & 255

            # OPEN SOCKET
            log.debug(
                "Opening stream socket to logger",
                extra={
                    "logger_sn": self.logger_sn,
                    "logger_ip": self.logger_ip,
                    "logger_port": self.logger_port,
                },
            )
            for res in socket.getaddrinfo(
                self.logger_ip, self.logger_port, socket.AF_INET, socket.SOCK_STREAM
            ):
                family, socktype, proto, canonname, sockadress = res
                try:
                    client_socket = socket.socket(family, socktype, proto)
                    client_socket.settimeout(10)
                    client_socket.connect(sockadress)
                except OSError as msg:
                    log.warning("Socket connect error", extra={"error": str(msg)})
                    return None

            if client_socket is None:
                return None

            # SEND DATA
            log.debug(
                "Sending data frame",
                extra={"frame_bytes": len(frame_bytes), "chunk_number": chunks},
            )
            client_socket.sendall(frame_bytes)

            # RECEIVE RESPONSE
            data = None
            try:
                data = client_socket.recv(1024)
                if data is None:
                    log.warning("No response data.")
                    return None
            except TimeoutError as msg:
                log.warning("Socket receive timeout", extra={"error": str(msg)})
                return None
            finally:
                try:
                    client_socket.close()
                except OSError as msg:
                    log.warning("Socket close error", extra={"error": str(msg)})

            log.debug(
                "Received chunk",
                extra={"data_bytes": len(data), "chunk_number": chunks},
            )
            # PARSE RESPONSE (start position 56, end position 60)
            totalpower = 0
            i = pfin - pini
            a = 0
            while a <= i:
                p1 = 56 + (a * 4)
                p2 = 60 + (a * 4)
                try:
                    response = twos_complement_hex(
                        str(
                            "".join(
                                hex(ord(chr(x)))[2:].zfill(2) for x in bytearray(data)
                            )
                            + "  "
                            + re.sub("[^\x20-\x7f]", "", "")
                        )[p1:p2]
                    )
                except ValueError:
                    log.warning(
                        "Discarding byte response",
                        exc_info=True,
                        extra={"response_bytes": len(data)},
                    )
                    return None
                hexpos = "0x" + str(hex(a + pini)[2:].zfill(4)).upper()
                for parameter in self.field_mappings:
                    for item in parameter["items"]:
                        title = item["titleEN"]
                        ratio = item["ratio"]
                        unit = item["unit"]
                        for register in item["registers"]:
                            if register == hexpos and chunks != -1:
                                if title.find("Temperature") != -1:
                                    response = round(response * ratio - 100, 2)
                                else:
                                    response = round(response * ratio, 2)
                                if len(unit) > 0:
                                    key = f"{title} {unit}"
                                else:
                                    key = f"{title}"
                                # sanitize string
                                key = (
                                    key.replace(" ", "_")
                                    .replace("-", "_")
                                    .replace("\u00ba", "c")
                                    .replace("%", "pct")
                                    .lower()
                                )
                                output[key] = response
                                if hexpos == "0x00BA":
                                    totalpower += response * ratio
                                if hexpos == "0x00BB":
                                    totalpower += response * ratio
                a += 1
            pini = 150
            pfin = 195
            chunks += 1
        log.debug(
            "Fetched fields",
            extra={"field_count": len(output), "chunk_count": chunks},
        )
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(
            "Using inverter logger",
            extra={
                "logger_sn": self.logger_sn,
                "logger_ip": self.logger_ip,
                "logger_port": self.logger_port,
            },
        )
        with exception_handler(
            connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True
        ) as app_socket:
            prev_battery_soc = None
            prev_battery_soc_set = time.time()
            while not threads.shutting_down:
                operation_start_time = time.time()
                tries = 1
                logger_data = None
                # try within the time budget to get a plausible value,
                # relative to the previous
                while (
                    time.time() - operation_start_time
                    < DEFAULT_SAMPLE_INTERVAL_SECONDS / 2
                ):
                    tries += 1
                    now = time.time()
                    logger_data = self.get_logger_data()
                    if isinstance(logger_data, dict):
                        if "battery_soc_pct" in logger_data.keys():
                            battery_soc = logger_data["battery_soc_pct"]
                            battery_voltage = logger_data["battery_voltage_v"]
                            # implausible battery state
                            if battery_soc == 0 and battery_voltage == 0:
                                log.warning(
                                    "Treating inverter output as implausible",
                                    extra={
                                        "battery_soc_pct": battery_soc,
                                        "battery_voltage_v": battery_voltage,
                                        "logger_data": str(logger_data),
                                    },
                                )
                                continue
                            # no previous to compare
                            if prev_battery_soc is None:
                                prev_battery_soc = battery_soc
                                prev_battery_soc_set = now
                                # current dict is good enough, break the try loop
                                break
                            soc_delta_pct = int(battery_soc - prev_battery_soc)
                            prev_battery_soc_last_set = now - prev_battery_soc_set
                            log.debug(
                                "battery_soc_pct changed",
                                extra={
                                    "soc_delta_pct": soc_delta_pct,
                                    "prev_battery_soc": prev_battery_soc,
                                    "prev_battery_soc_set_secs_ago": round(
                                        prev_battery_soc_last_set, 2
                                    ),
                                    "battery_soc": battery_soc,
                                },
                            )
                            # check for an implausible negative change within
                            # some time bound
                            if (
                                abs(soc_delta_pct) >= IMPLAUSIBLE_CHANGE_PERCENTAGE
                                and prev_battery_soc_last_set
                                < DEFAULT_SAMPLE_INTERVAL_SECONDS * 2
                            ):
                                log.warning(
                                    "Treating battery_soc_pct change as implausible",
                                    extra={
                                        "max_change_pct": IMPLAUSIBLE_CHANGE_PERCENTAGE,
                                        "prev_battery_soc": prev_battery_soc,
                                        "battery_soc": battery_soc,
                                        "logger_data": str(logger_data),
                                    },
                                )
                            else:
                                # accept the new value as good
                                prev_battery_soc = battery_soc
                                prev_battery_soc_set = now
                                # control field change is plausible
                                break
                    log.warning(
                        "Waiting after unsuccessful tries",
                        extra={
                            "retry_interval_secs": ERROR_RETRY_INTERVAL_SECONDS,
                            "tries": tries,
                        },
                    )
                    threads.interruptable_sleep.wait(ERROR_RETRY_INTERVAL_SECONDS)
                if logger_data is not None and len(logger_data) > 0:
                    log.debug(
                        "Sending fields for publication",
                        extra={"field_count": len(logger_data)},
                    )
                    app_socket.send_pyobj({"inverter": logger_data})
                else:
                    log.warning(
                        "Unable to fetch any valid data",
                        extra={
                            "tries": tries,
                            "interval_secs": DEFAULT_SAMPLE_INTERVAL_SECONDS,
                        },
                    )
                # stop for the remainder of the sampling interval
                operation_time = time.time() - operation_start_time
                sample_delay = self.sample_interval_secs - operation_time
                if sample_delay < 0:
                    normalized_sample_delay = min(
                        operation_time, self.sample_interval_secs
                    )
                    log.warning(
                        "Sample interval is too short. Resetting delay",
                        extra={
                            "sample_interval_secs": self.sample_interval_secs,
                            "implied_wait_secs": round(sample_delay, 2),
                            "normalized_delay_secs": round(normalized_sample_delay, 2),
                        },
                    )
                    # don't use 0: never spin
                    sample_delay = normalized_sample_delay
                log.debug(
                    "Waiting until the next sample",
                    extra={"sample_delay_secs": round(sample_delay, 2)},
                )
                threads.interruptable_sleep.wait(sample_delay)


class WeatherReader(AppThread):
    def __init__(self):
        global creds
        AppThread.__init__(self, name=self.__class__.__name__)
        if creds is None:
            raise RuntimeError("Credentials not initialized")
        self.api_key = creds.get_creds("OpenWeather/password")
        self.lat, self.lon = tuple(
            app_config.get("weather", "coord_lat_lon").split(",")
        )

    def get_weather_data(self):
        output = None
        try:
            r = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "lat": self.lat,
                    "lon": self.lon,
                    "appid": self.api_key,
                },
            )
            try:
                output = json.loads(r.content)
                log.debug("Loaded weather fields", extra={"field_count": len(output)})
            except JSONDecodeError:
                log.warning(
                    "JSON parse error of weather response",
                    exc_info=True,
                    extra={"response_content": repr(r.content)},
                )
                return None
        except OSError, ConnectionError, RequestException:
            log.warning("Problem getting weather data.", exc_info=True)
            return None
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(
            "Fetching weather data using coordinates",
            extra={"lat": self.lat, "lon": self.lon},
        )
        with exception_handler(
            connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True
        ) as app_socket:
            while not threads.shutting_down:
                wd = self.get_weather_data()
                log.debug("Received weather data", extra={"weather_data": wd})
                if wd is not None and len(wd) > 0:
                    weather = dict()
                    weather["cloudiness_pct"] = wd["clouds"]["all"]
                    date_value = int(wd["dt"])
                    sunrise = int(wd["sys"]["sunrise"])
                    sunset = int(wd["sys"]["sunset"])
                    sun_output = 0
                    # calculate theoretical sun output
                    if date_value > sunrise and date_value < sunset:
                        # normalize and divide
                        midday_secs = (sunset - sunrise) / 2
                        secs_from_dark = min(date_value - sunrise, sunset - date_value)
                        sun_output = int((secs_from_dark / midday_secs) * 100)
                        log.debug(
                            "Derived sun output",
                            extra={
                                "sun_output_pct": sun_output,
                                "sunrise": sunrise,
                                "date_value": date_value,
                                "sunset": sunset,
                                "midday_secs": midday_secs,
                                "secs_from_dark": secs_from_dark,
                            },
                        )
                    else:
                        log.debug(
                            "Using sun output",
                            extra={
                                "sun_output_pct": sun_output,
                                "sunrise": sunrise,
                                "date_value": date_value,
                                "sunset": sunset,
                            },
                        )
                    country = wd["sys"]["country"]
                    weather["midday_pct"] = sun_output
                    log.debug(
                        "Sending weather fields for publication",
                        extra={
                            "country": country,
                            "field_count": len(weather),
                            "weather": weather,
                        },
                    )
                    app_socket.send_pyobj({"weather": weather})
                threads.interruptable_sleep.wait(DEFAULT_SAMPLE_INTERVAL_SECONDS)


class BmsReader(AppThread):
    def __init__(self, port="/dev/ttyUSB1", baudrate=9600):
        AppThread.__init__(self, name=self.__class__.__name__)
        global creds
        self.port = port
        self.baudrate = baudrate
        self.reader = None
        self.bms_data = {}
        self._bms_id_counter = 0
        self._addr_to_name = {}

        # PagerDuty alerting state via Events API V2 client
        self.pd_client: EventsApiV2Client | None = None
        if app_config.getboolean("app", "paging_enabled"):
            if creds is None:
                raise RuntimeError("Credentials not initialized")
            self.pd_client = EventsApiV2Client(
                routing_key=creds.get_creds("PagerDuty.inverter-monitor/routing_key")
            )
        self.pd_dedup_key: str | None = None
        self.pd_alert_triggered = False
        self.last_bms_data_time = 0.0
        self._startup_time = 0.0

        # Per-address last-seen timestamps for minimum count check
        self._bms_last_seen: dict[int, float] = {}
        self._minimum_bms_count = app_config.getint(
            "alert_thresholds", "minimum_bms_count"
        )
        self.pd_count_dedup_key: str | None = None
        self.pd_count_alert_triggered = False

    @staticmethod
    def _extract_scalars(data: dict) -> tuple[dict, list[float]]:
        """Extract scalar battery metrics and cell voltage array.

        Returns (scalars_dict, cells_v_list).
        Scalar dict contains all single-value metrics suitable for a
        labeled time series.
        Array fields (cells_v, all temps) are excluded and returned separately.
        """
        scalars: dict = {}
        cells_v: list[float] = data.get("cells_v", [])

        if data.get("voltage_v") is not None:
            scalars["voltage_v"] = data["voltage_v"]
        if data.get("cell_count") is not None:
            scalars["cell_count"] = data["cell_count"]
        if data.get("min_cell_v") is not None:
            scalars["min_cell_v"] = data["min_cell_v"]
        if data.get("max_cell_v") is not None:
            scalars["max_cell_v"] = data["max_cell_v"]
        if data.get("cell_diff_mv") is not None:
            scalars["cell_diff_mv"] = data["cell_diff_mv"]
        if data.get("min_cell_idx") is not None:
            scalars["min_cell_idx"] = data["min_cell_idx"]
        if data.get("max_cell_idx") is not None:
            scalars["max_cell_idx"] = data["max_cell_idx"]
        if data.get("charging") is not None:
            scalars["charging"] = data["charging"]
        if data.get("discharging") is not None:
            scalars["discharging"] = data["discharging"]
        if data.get("capacity_raw_1") is not None:
            scalars["capacity_raw_1"] = data["capacity_raw_1"]
        if data.get("capacity_raw_2") is not None:
            scalars["capacity_raw_2"] = data["capacity_raw_2"]

        # Extra temperature sensor
        extra_temp = data.get("extra_temp")
        if extra_temp and extra_temp.get("celsius") is not None:
            scalars["extra_temp_c"] = extra_temp["celsius"]

        # Derive current from status flags and raw current
        current_raw = data.get("current_raw")
        if current_raw is not None:
            idle_baseline = 1681
            current_a = round((current_raw - idle_baseline) * 0.01, 2)
            if data.get("discharging"):
                current_a = -abs(current_a)
            scalars["current_a"] = current_a

        # Named temperature fields by position
        temps = data.get("temps", [])
        temp_c_values = [
            t.get("celsius") for t in temps if t.get("celsius") is not None
        ]
        if len(temp_c_values) > 0:
            scalars["battery_temp_c"] = temp_c_values[0]
        if len(temp_c_values) > 1:
            scalars["mos_temp_c"] = temp_c_values[1]

        return scalars, cells_v

    # noinspection PyBroadException
    def run(self):
        log.info(
            "Starting BMS reader",
            extra={"port": self.port, "baudrate": self.baudrate},
        )
        self.reader = SerialPortReader(port=self.port, baudrate=self.baudrate)

        if not self.reader.connect():
            log.error("Could not connect to BMS serial port", extra={"port": self.port})
            return

        log.info("Connected to BMS", extra={"port": self.port})
        # Frames are queued internally; we pull them from the main thread.
        self.reader.start(on_frame=None)

        # Initialize heartbeat timer so alert fires if no data arrives within 60s
        self.last_bms_data_time = time.time()
        self._startup_time = self.last_bms_data_time

        # Seed PagerDuty state so any previously-open incidents
        # auto-resolve on first data
        if self.pd_client is not None:
            self.pd_alert_triggered = True
            self.pd_dedup_key = "bms_heartbeat"
            self.pd_count_alert_triggered = True
            self.pd_count_dedup_key = "bms_count"

        with exception_handler(
            connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True
        ) as app_socket:
            seen_addresses = set()
            while not threads.shutting_down:
                if not self.reader.is_connected:
                    log.error("BMS serial connection lost.")
                    break

                # Block until a valid, decoded frame arrives
                # (or timeout to check shutdown flag)
                data = self.reader.get_result(timeout=1.0)
                if data is None:
                    # Check for data-loss timeout and trigger PagerDuty if needed
                    if (
                        self.last_bms_data_time > 0
                        and time.time() - self.last_bms_data_time
                        > BMS_DATA_LOSS_TIMEOUT
                        and not self.pd_alert_triggered
                    ):
                        if self.pd_client is not None:
                            try:
                                self.pd_dedup_key = self.pd_client.trigger(
                                    dedup_key="bms_heartbeat",
                                    summary=(
                                        f"BMS serial data loss on {self.port} "
                                        f"\u2014 no frames received for "
                                        f">{BMS_DATA_LOSS_TIMEOUT} seconds"
                                    ),
                                    source=str(DEVICE_NAME_BASE),
                                    severity="warning",
                                )
                                self.pd_alert_triggered = True
                                log.warning(
                                    "PagerDuty alert triggered for BMS data loss",
                                    extra={"dedup_key": self.pd_dedup_key},
                                )
                            except Exception:
                                log.warning("PagerDuty trigger failed.", exc_info=True)
                        else:
                            log.warning(
                                "PagerDuty not configured; cannot trigger"
                                " alert for BMS data loss."
                            )
                    continue

                addr = data.get("addr")
                if addr is None:
                    continue

                # Assign a friendly BMS name on first sight
                if addr not in self._addr_to_name:
                    self._bms_id_counter += 1
                    self._addr_to_name[addr] = f"BMS{self._bms_id_counter:02d}"
                bms_name = self._addr_to_name[addr]

                # Extract scalars and raw cell voltage list
                scalars, cells_v = self._extract_scalars(data)

                # Build a compatible dict for logging/PagerDuty/cache
                bms_info = {
                    "addr": addr,
                    "voltage_v": scalars.get("voltage_v", 0),
                    "cell_count": scalars.get("cell_count", 0),
                    "min_cell_v": scalars.get("min_cell_v", 0),
                    "max_cell_v": scalars.get("max_cell_v", 0),
                    "cell_diff_mv": scalars.get("cell_diff_mv", 0),
                    "model": data.get("model", ""),
                    "serial": data.get("serial", ""),
                }

                # Record successful frame time and per-address last-seen
                self.last_bms_data_time = time.time()
                self._bms_last_seen[addr] = self.last_bms_data_time

                # Resolve any open PagerDuty incidents
                if self.pd_alert_triggered:
                    if self.pd_client is not None:
                        try:
                            if self.pd_dedup_key is not None:
                                self.pd_client.resolve(
                                    dedup_key=self.pd_dedup_key,
                                )
                            self.pd_alert_triggered = False
                            log.info("PagerDuty heartbeat incident resolved.")
                        except Exception:
                            log.warning(
                                "PagerDuty heartbeat resolve failed.", exc_info=True
                            )
                    else:
                        log.warning(
                            "PagerDuty not configured; cannot resolve"
                            " alert for BMS data restoration."
                        )
                    # Also resolve count alert when data flow resumes
                    if self.pd_count_alert_triggered and self.pd_client is not None:
                        try:
                            if self.pd_count_dedup_key is not None:
                                self.pd_client.resolve(
                                    dedup_key=self.pd_count_dedup_key,
                                )
                            self.pd_count_alert_triggered = False
                            log.info(
                                "PagerDuty count incident resolved (data restored).",
                            )
                        except Exception:
                            log.warning(
                                "PagerDuty count resolve failed.", exc_info=True
                            )

                # Log newly detected packs
                if addr not in seen_addresses:
                    seen_addresses.add(addr)
                    log.info(
                        "New BMS detected",
                        extra={
                            "bms_name": bms_name,
                            "addr": addr,
                            "cell_count": bms_info.get("cell_count", 0),
                            "voltage_v": bms_info.get("voltage_v", 0),
                            "model": data.get("model", ""),
                            "serial": data.get("serial", ""),
                        },
                    )

                # Log per-frame summary
                log.debug(
                    "BMS frame summary",
                    extra={
                        "bms_name": bms_name,
                        "addr": addr,
                        "cell_count": bms_info.get("cell_count", 0),
                        "voltage_v": bms_info.get("voltage_v", 0),
                        "min_cell_v": bms_info.get("min_cell_v", 0),
                        "max_cell_v": bms_info.get("max_cell_v", 0),
                        "cell_diff_mv": bms_info.get("cell_diff_mv", 0),
                    },
                )

                # Cache latest data per address (keep for compatibility)
                self.bms_data[addr] = scalars

                # Send labeled battery-level scalars
                battery_metrics = [
                    {"labels": {"bms_addr": bms_name}, "metrics": scalars}
                ]
                log.debug(
                    "Sending BMS frame for publication",
                    extra={"bms_name": bms_name, "scalar_count": len(scalars)},
                )
                app_socket.send_pyobj({"battery": battery_metrics})

                # Send per-cell voltage as a separate labeled point
                if cells_v:
                    cell_metrics = []
                    for idx, cell_v in enumerate(cells_v):
                        cell_metrics.append(
                            {
                                "labels": {
                                    "bms_addr": bms_name,
                                    "cell": f"{idx + 1:02d}",
                                },
                                "metrics": {"voltage_v": cell_v},
                            }
                        )
                    app_socket.send_pyobj({"bms_cell": cell_metrics})

                # Check if enough distinct BMS units have reported recently
                now = time.time()
                active_addrs = sum(
                    1
                    for ts in self._bms_last_seen.values()
                    if now - ts < BMS_DATA_LOSS_TIMEOUT
                )
                if (
                    active_addrs < self._minimum_bms_count
                    and not self.pd_count_alert_triggered
                    and time.time() - self._startup_time >= 300
                ):
                    if self.pd_client is not None:
                        try:
                            self.pd_count_dedup_key = self.pd_client.trigger(
                                dedup_key="bms_count",
                                summary=(
                                    f"Only {active_addrs}/"
                                    f"{self._minimum_bms_count} BMS units "
                                    f"reporting on {self.port}"
                                ),
                                source=str(DEVICE_NAME_BASE),
                                severity="warning",
                            )
                            self.pd_count_alert_triggered = True
                            log.warning(
                                "PagerDuty alert triggered for low BMS count",
                                extra={"dedup_key": self.pd_count_dedup_key},
                            )
                        except Exception:
                            log.warning(
                                "PagerDuty count trigger failed.", exc_info=True
                            )
                    else:
                        log.warning(
                            "PagerDuty not configured; cannot trigger"
                            " alert for low BMS count."
                        )
                elif (
                    active_addrs >= self._minimum_bms_count
                    and self.pd_count_alert_triggered
                ):
                    if self.pd_client is not None:
                        try:
                            if self.pd_count_dedup_key is not None:
                                self.pd_client.resolve(
                                    dedup_key=self.pd_count_dedup_key,
                                )
                            self.pd_count_alert_triggered = False
                            log.info("PagerDuty count incident resolved.")
                        except Exception:
                            log.warning(
                                "PagerDuty count resolve failed.", exc_info=True
                            )
                    else:
                        log.warning(
                            "PagerDuty not configured; cannot resolve"
                            " alert for BMS count restoration."
                        )

        self.reader.stop()
        self.reader.disconnect()


class MqttSubscriber(AppThread, Closable):
    def __init__(self, mqtt_server_address, mqtt_topic_prefix, mqtt_switch_devices):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_MQTT_PUBLISH)

        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_server_address = mqtt_server_address
        self._mqtt_subscribe_topic_prefix = mqtt_topic_prefix
        self._mqtt_switch_devices = mqtt_switch_devices

        self._disconnected = False

        self._switch_state = dict()

        self._power_generation_history: deque[float] = deque(maxlen=5)

    def close(self):
        Closable.close(self)
        try:
            if self._mqtt_client is not None:
                self._mqtt_client.disconnect()
        except Exception:
            log.warning("Ignoring error closing MQTT socket.", exc_info=True)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        subscription_topic = f"{self._mqtt_subscribe_topic_prefix}/state/#"
        log.info(
            "Subscribing to topic", extra={"subscription_topic": subscription_topic}
        )
        if self._mqtt_client is not None:
            self._mqtt_client.subscribe(subscription_topic)

    def on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        log.info("MQTT client has disconnected.")
        self._disconnected = True

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload
        log.debug(
            "MQTT message received",
            extra={"topic": topic, "payload_bytes": len(payload)},
        )
        msg_data = None
        try:
            log.debug(
                "MQTT payload received", extra={"topic": topic, "payload": payload}
            )
            msg_data = json.loads(payload)
        except JSONDecodeError:
            log.exception("Unstructured message", extra={"payload": payload})
            return
        except ContextTerminated:
            self.close()
        if msg_data is not None and "switches" in msg_data.keys():
            switch_bank = topic.split("/")[2]
            new_state = msg_data["switches"]
            old_state = list()
            if switch_bank in self._switch_state:
                old_state = self._switch_state[switch_bank]
            if new_state != old_state:
                for ids, s in enumerate(new_state):
                    log.info(
                        "Switch state changed",
                        extra={
                            "switch_bank": switch_bank,
                            "switch_number": ids + 1,
                            "state": s,
                        },
                    )
            # state capture
            self._switch_state[switch_bank] = new_state

    def set_switch_state(self, switch_state=1):
        for switch_bank in self._switch_state.keys():
            if switch_bank not in self._mqtt_switch_devices:
                log.warning(
                    "Not changing switch state due to missing configuration",
                    extra={"switch_bank": switch_bank},
                )
                continue
            mqtt_pub_topic = "/".join(
                [f"{self._mqtt_subscribe_topic_prefix}", "control", switch_bank]
            )
            mqtt_update = list()
            for _ids, _ in enumerate(self._switch_state[switch_bank]):
                mqtt_update.append(switch_state)
            message_data = json.dumps({"state": mqtt_update})
            with OTEL_TRACER.start_as_current_span(
                "mqtt.publish", kind=SpanKind.PRODUCER
            ) as span:
                span.set_attribute("messaging.system", "mqtt")
                span.set_attribute("messaging.destination.name", mqtt_pub_topic)
                span.set_attribute("messaging.destination_kind", "topic")
                span.set_attribute("messaging.message.body.size", len(message_data))
                tp = format_traceparent(span)
                span.set_attribute("traceparent", tp)
                payload_obj = {"state": mqtt_update, "traceparent": tp}
                message_data = json.dumps(payload_obj)
                if self._mqtt_client is not None:
                    self._mqtt_client.publish(
                        topic=mqtt_pub_topic, payload=message_data
                    )
                log.info(
                    "MQTT message dispatched",
                    extra={
                        "topic": mqtt_pub_topic,
                        "message_bytes": len(message_data),
                        "traceparent": tp,
                    },
                )

    def get_power_generation_avg(self, value):
        self._power_generation_history.append(value)
        total: float = 0.0
        for sample in self._power_generation_history:
            total += sample
        return total / len(self._power_generation_history)

    # noinspection PyBroadException
    def run(self):
        log.info(
            "Connecting to MQTT server",
            extra={"mqtt_server_address": self._mqtt_server_address},
        )
        self._mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        self._mqtt_client.on_connect = self.on_connect
        self._mqtt_client.on_disconnect = self.on_disconnect
        self._mqtt_client.on_message = self.on_message
        self._mqtt_client.connect(self._mqtt_server_address)
        my_socket = self.get_socket()
        with exception_handler(
            connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True
        ) as app_socket:
            while not threads.shutting_down:
                switch_stats = dict()
                rc = self._mqtt_client.loop()
                if rc == MQTT_ERR_NO_CONN or self._disconnected:
                    raise ResourceWarning(
                        f"No connection to MQTT broker at "
                        f"{self._mqtt_server_address} "
                        f"(disconnected? {self._disconnected})"
                    )
                inverter_data = None
                # check for messages to publish
                try:
                    inverter_data = my_socket.recv_pyobj(flags=zmq.NOBLOCK)
                except ZMQError:
                    # ignore, no data
                    continue
                if not isinstance(inverter_data, dict):
                    continue
                # check for required fields
                if not all(
                    field in inverter_data.keys()
                    for field in [
                        "alert",
                        "battery_power_w",
                        "pv1_power_w",
                        "pv2_power_w",
                    ]
                ):
                    continue
                switch_state = 1
                switch_stats["surplus_ration"] = 0
                switch_stats["battery_ration"] = 0
                if int(inverter_data["alert"]) == 1:
                    # do not load shed during an alert condition
                    self.set_switch_state()
                    continue
                # check 1: calculate surplus as a function of PV reported *usage*
                # and how much the batteries are supplying
                pv1_power_w = float(inverter_data["pv1_power_w"])
                pv2_power_w = float(inverter_data["pv2_power_w"])
                battery_power_w = float(inverter_data["battery_power_w"])
                power_generation_w_avg = self.get_power_generation_avg(
                    value=pv1_power_w + pv2_power_w - battery_power_w
                )
                # disable switch if battery is critically low without
                # adequate surplus (i.e. not charging from solar)
                battery_soc_pct = inverter_data["battery_soc_pct"]
                if (
                    battery_soc_pct < BATTERY_CRITICAL_PCT
                    and power_generation_w_avg < 0
                ):
                    switch_state = 0
                    switch_stats["surplus_ration"] = 1
                # check 2: determine battery state of charge and
                # whether there is any grid fallback
                grid_voltage_l1_v = float(inverter_data["grid_voltage_l1_v"])
                grid_voltage_l2_v = float(inverter_data["grid_voltage_l2_v"])
                grid_voltage = max(grid_voltage_l1_v, grid_voltage_l2_v)
                # more conservative rationing if no grid backup
                # (draw assumes no surplus)
                if (
                    battery_soc_pct < BATTERY_LOW_PCT
                    and grid_voltage < 90
                    and battery_power_w >= BATTERY_MAJOR_DRAW_W
                ):
                    switch_state = 0
                    switch_stats["battery_ration"] = 1
                # check 3: determine whether the inverter is no longer
                # pulling from solar or battery (i.e. from grid)
                inverter_l1_power_w = float(inverter_data["inverter_l1_power_w"])
                inverter_l2_power_w = float(inverter_data["inverter_l2_power_w"])
                # can't use min/max because l2 is normally 0
                inverter_power_w = inverter_l1_power_w + inverter_l2_power_w
                if inverter_power_w < 0:
                    switch_state = 0
                    switch_stats["battery_ration"] = 1
                # log the supporting data
                log_msg = (
                    "Inverter is delivering power to consumers from backup "
                    "(solar/battery)"
                )
                log_fields = {
                    "inverter_power_w": inverter_power_w,
                    "power_generation_w_avg": round(power_generation_w_avg, 2),
                    "pv1_power_w": round(pv1_power_w, 2),
                    "pv2_power_w": round(pv2_power_w, 2),
                    "battery_power_w": round(battery_power_w, 2),
                    "battery_soc_pct": battery_soc_pct,
                    "grid_voltage_v": grid_voltage,
                    "switch_state": switch_state,
                }
                log.debug(log_msg, extra=log_fields)
                # update switches
                self.set_switch_state(switch_state=switch_state)
                # post stats
                switch_stats["switch_state"] = switch_state
                app_socket.send_pyobj({"switches": switch_stats})
                # for other interested consumers
                if self._mqtt_client is not None:
                    with OTEL_TRACER.start_as_current_span(
                        "mqtt.publish", kind=SpanKind.PRODUCER
                    ) as span:
                        span.set_attribute("messaging.system", "mqtt")
                        span.set_attribute(
                            "messaging.destination.name", "inverter/state"
                        )
                        span.set_attribute("messaging.destination_kind", "topic")
                        tp = format_traceparent(span)
                        span.set_attribute("traceparent", tp)
                        inverter_data["traceparent"] = tp
                        payload = json.dumps(inverter_data)
                        span.set_attribute("messaging.message.body.size", len(payload))
                        self._mqtt_client.publish(
                            topic="inverter/state", payload=payload
                        )
                        log.info(
                            "MQTT message dispatched",
                            extra={
                                "topic": "inverter/state",
                                "traceparent": tp,
                                "message_bytes": len(payload),
                            },
                        )
        self.close()


class EventProcessor(AppThread, Closable):
    def __init__(self, debug_metrics, telegram_enabled=False):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_APP)

        self.debug_metrics = debug_metrics
        self._telegram_enabled = telegram_enabled

    # noinspection PyBroadException
    def run(self):
        log.debug(
            "Debug metrics configured", extra={"debug_metrics": self.debug_metrics}
        )
        my_socket = self.get_socket()
        self._gauges: dict = {}
        # Set up Telegram bot fan-out PUSH socket
        telegram_socket = None
        if self._telegram_enabled:
            from tailucas_pylib.zmq import zmq_socket

            telegram_socket = zmq_socket(socket_type=zmq.PUSH)
            telegram_socket.connect(URL_WORKER_TELEGRAM)
            log.info("Telegram bot fan-out enabled")
        with exception_handler(
            connect_url=URL_WORKER_MQTT_PUBLISH, and_raise=False, shutdown_on_error=True
        ) as mqtt_socket:
            while not threads.shutting_down:
                event = my_socket.recv_pyobj()
                log.debug("Event received", extra={"event": event})
                if isinstance(event, dict):
                    for point_name in list(event):
                        point_items = event[point_name]
                        if isinstance(point_items, list):
                            # Labeled format: list of
                            # {"labels": {...}, "metrics": {...}}
                            for entry in point_items:
                                labels = entry.get("labels", {})
                                metrics = entry.get("metrics", {})
                                if point_name in self.debug_metrics:
                                    log.debug(
                                        "Log-only Metric",
                                        extra={
                                            "point_name": point_name,
                                            "labels": labels,
                                            "metrics": metrics,
                                        },
                                    )
                                    continue
                                for metric_key, metric_value in metrics.items():
                                    if not isinstance(metric_value, (int, float, bool)):
                                        continue
                                    # OTEL NumberDataPoint cannot encode
                                    # booleans; coerce to 0/1 for the gauge
                                    gauge_value = metric_value
                                    if isinstance(metric_value, bool):
                                        gauge_value = int(metric_value)
                                    gauge_key = f"{point_name}_{metric_key}"
                                    if gauge_key not in self._gauges:
                                        self._gauges[gauge_key] = (
                                            OTEL_METER.create_gauge(
                                                name=gauge_key,
                                                description=(
                                                    f"{point_name} {metric_key}"
                                                ),
                                            )
                                        )
                                    try:
                                        attrs = {
                                            str(k): str(v) for k, v in labels.items()
                                        }
                                        if attrs:
                                            log.debug(
                                                "Setting gauge",
                                                extra={
                                                    "gauge_key": gauge_key,
                                                    "attributes": attrs,
                                                    "metric_value": gauge_value,
                                                },
                                            )
                                            self._gauges[gauge_key].set(
                                                gauge_value, attributes=attrs
                                            )
                                        else:
                                            self._gauges[gauge_key].set(gauge_value)
                                    except ValueError as e:
                                        log.warning(
                                            "Invalid value for gauge",
                                            extra={
                                                "gauge_key": gauge_key,
                                                "value": gauge_value,
                                                "error": str(e),
                                            },
                                        )
                        elif isinstance(point_items, dict):
                            # Legacy flat format
                            for key, value in point_items.items():
                                if point_name in self.debug_metrics:
                                    log.debug(
                                        "Log-only Metric",
                                        extra={
                                            "point_name": point_name,
                                            "field_key": key,
                                            "value": value,
                                        },
                                    )
                                    continue
                                gauge_name = key
                                if key not in self._gauges:
                                    if not gauge_name.startswith(point_name):
                                        gauge_name = f"{point_name}_{key}"
                                    self._gauges[key] = OTEL_METER.create_gauge(
                                        name=gauge_name,
                                        description=f"{point_name} {key}",
                                    )
                                # OTEL NumberDataPoint cannot encode
                                # booleans; coerce to 0/1 for the gauge
                                gauge_value = value
                                if isinstance(value, bool):
                                    gauge_value = int(value)
                                try:
                                    self._gauges[key].set(gauge_value)
                                except ValueError as e:
                                    log.warning(
                                        "Invalid value for gauge",
                                        extra={
                                            "gauge_name": gauge_name,
                                            "value": gauge_value,
                                            "error": str(e),
                                        },
                                    )
                        else:
                            log.warning(
                                "Unexpected point_items type",
                                extra={
                                    "point_name": point_name,
                                    "point_items_type": str(type(point_items)),
                                },
                            )
                        if point_name == "inverter" and point_name not in debug_metrics:
                            mqtt_socket.send_pyobj(point_items)
                        # Forward inverter and battery to Telegram bot when enabled
                        if telegram_socket is not None:
                            if point_name == "inverter" and isinstance(
                                point_items, dict
                            ):
                                telegram_socket.send_pyobj({"inverter": point_items})
                            elif point_name == "battery" and isinstance(
                                point_items, list
                            ):
                                telegram_socket.send_pyobj({"battery": point_items})
        self.close()


def main():
    global creds
    global debug_metrics
    creds = Creds()
    creds.validate_creds()

    # Reduce Sentry noise from Telegram/async libraries
    ignore_logger("telegram.ext.Updater")
    ignore_logger("telegram.ext._updater")
    ignore_logger("asyncio")

    # load basic configuration
    app_path = Path(os.path.abspath(os.path.dirname(__file__))).parent
    mappings = None
    mappings_file = os.path.join(app_path, "config", "field_mappings.txt")
    with open(mappings_file) as mapping_file:
        try:
            mappings = json.loads(mapping_file.read())
            log.info(
                "Loaded field mappings",
                extra={"mapping_count": len(mappings), "mappings_file": mappings_file},
            )
        except JSONDecodeError as e:
            log.exception(
                "Error loading field mappings", extra={"mappings_file": mappings_file}
            )
            raise e
    # load time series clients
    # Extract Prometheus credentials before the event loop starts
    prom_url = ""
    prom_user = ""
    prom_token = ""
    try:
        prom_url = creds.get_creds(f"Prometheus/{APP_NAME}/url")
    except Exception:
        pass
    try:
        prom_user = creds.get_creds(f"Prometheus/{APP_NAME}/user")
    except Exception:
        pass
    try:
        prom_token = creds.get_creds(f"Prometheus/{APP_NAME}/token")
    except Exception:
        pass
    log.info(
        "Prometheus configured",
        extra={
            "prometheus_url": prom_url,
            "prometheus_user": prom_user,
            "prometheus_token_set": bool(prom_token),
        },
    )
    metrics_configure(url=prom_url, user=prom_user, token=prom_token)
    # ensure proper signal handling; must be main thread
    signal_handler = SignalHandler()
    telegram_enabled = is_flag_enabled("telegram-bot")
    event_processor = EventProcessor(
        debug_metrics=debug_metrics,
        telegram_enabled=telegram_enabled,
    )
    logger_reader: LoggerReader | None = None
    if app_config.getboolean("inverter", "logging_enabled"):
        logger_reader = LoggerReader(
            field_mappings=mappings,
            logger_sn=app_config.getint("inverter", "logger_sn"),
            logger_ip=app_config.get("inverter", "logger_address"),
            logger_port=app_config.getint("inverter", "logger_port"),
            sample_interval_secs=app_config.getint(
                "inverter", "logger_sample_interval_seconds"
            ),
        )
    bms_reader: BmsReader | None = None
    if app_config.getboolean("bms", "logging_enabled"):
        bms_reader = BmsReader(
            port=app_config.get("bms", "serial_port", fallback="/dev/ttyUSB1"),
        )
    weather_reader = WeatherReader()
    mqtt_subscriber = MqttSubscriber(
        mqtt_server_address=app_config.get("mqtt", "server_address"),
        mqtt_topic_prefix=app_config.get("mqtt", "topic_prefix"),
        mqtt_switch_devices=app_config.get("mqtt", "switch_device_csv").split(","),
    )
    telegram_bot: TelegramBot | None = None
    if telegram_enabled:
        from app.bot import TelegramBot

        telegram_bot = TelegramBot(creds_obj=creds)
    else:
        log.warning("Telegram bot is disabled.")
    nanny = threading.Thread(
        name="nanny", target=thread_nanny, args=(signal_handler,), daemon=True
    )
    # startup completed
    try:
        log.info("Starting application threads", extra={"app_name": APP_NAME})
        event_processor.start()
        if logger_reader is not None:
            logger_reader.start()
        else:
            log.warning("Inverter logger reader is disabled.")
        if bms_reader is not None:
            bms_reader.start()
        else:
            log.warning("BMS reader is disabled.")
        weather_reader.start()
        mqtt_subscriber.start()
        if telegram_bot is not None:
            telegram_bot.start()
        # start thread nanny
        nanny.start()
        log.info("Startup complete.")
        # hang around until something goes wrong
        threads.interruptable_sleep.wait()
        raise RuntimeWarning("Shutting down...")
    except KeyboardInterrupt, RuntimeWarning, ContextTerminated:
        die()
    finally:
        tracing.shutdown()
        zmq_term()
    bye()


if __name__ == "__main__":
    main()
