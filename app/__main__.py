#!/usr/bin/env python
import binascii
import logging.handlers
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
import sentry_sdk
import simplejson as json
import zmq
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS
from pagerduty import EventsApiV2Client
from paho.mqtt.client import MQTT_ERR_NO_CONN
from prometheus_client import CollectorRegistry, Gauge, multiprocess, start_http_server
from requests.adapters import ConnectionError
from requests.exceptions import RequestException
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.sys_exit import SysExitIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration
from simplejson.scanner import JSONDecodeError
from tailucas_pylib import APP_NAME, DEVICE_NAME_BASE, app_config, log, threads
from tailucas_pylib.app import AppThread
from tailucas_pylib.creds import Creds
from tailucas_pylib.flags import is_flag_enabled
from tailucas_pylib.handler import exception_handler
from tailucas_pylib.process import SignalHandler
from tailucas_pylib.threads import bye, die, thread_nanny
from tailucas_pylib.zmq import URL_WORKER_APP, Closable, zmq_term
from zmq.error import ContextTerminated, ZMQError

from app.serial_reader import SerialPortReader

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
                f"Opening stream socket to logger {self.logger_sn} @ {self.logger_ip}:{self.logger_port}..."
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
                    log.warning(f"{msg}")
                    return None

            if client_socket is None:
                return None

            # SEND DATA
            log.debug(
                f"Sending {len(frame_bytes)} bytes data frame for chunk {chunks}."
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
                log.warning(f"{msg}")
                return None
            finally:
                try:
                    client_socket.close()
                except OSError as msg:
                    log.warning(f"{msg}")

            log.debug(f"Received {len(data)} bytes for chunk {chunks}.")
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
                    log.warning(f"Discarding {len(data)} byte response.", exc_info=True)
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
        log.debug(f"Fetched {len(output)} fields after {chunks} chunks.")
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(
            f"Using inverter logger {self.logger_sn} at address {self.logger_ip}:{self.logger_port}."
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
                # try within the time budget to get a plausible value, relative to the previous
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
                                    f"{battery_soc=}% and {battery_voltage=}v. Treating this output as implausible: {str(logger_data)}"
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
                                f"battery_soc_pct changed by {soc_delta_pct}% from {prev_battery_soc} (set {prev_battery_soc_last_set:.2f}s ago) to {battery_soc}."
                            )
                            # check for an implausible negative change within some time bound
                            if (
                                abs(soc_delta_pct) >= IMPLAUSIBLE_CHANGE_PERCENTAGE
                                and prev_battery_soc_last_set
                                < DEFAULT_SAMPLE_INTERVAL_SECONDS * 2
                            ):
                                log.warning(
                                    f"battery_soc_pct changed by more than {IMPLAUSIBLE_CHANGE_PERCENTAGE}% from {prev_battery_soc} to {battery_soc}. Treating this output as implausible: {str(logger_data)}"
                                )
                            else:
                                # accept the new value as good
                                prev_battery_soc = battery_soc
                                prev_battery_soc_set = now
                                # control field change is plausible
                                break
                    log.warning(
                        f"Waiting {ERROR_RETRY_INTERVAL_SECONDS}s after {tries} unsuccessful tries."
                    )
                    threads.interruptable_sleep.wait(ERROR_RETRY_INTERVAL_SECONDS)
                if logger_data is not None and len(logger_data) > 0:
                    log.debug(f"Sending {len(logger_data)} fields for publication.")
                    app_socket.send_pyobj({"inverter": logger_data})
                else:
                    log.warning(
                        f"Unable to fetch any valid data after {tries} tries (within {DEFAULT_SAMPLE_INTERVAL_SECONDS}s)."
                    )
                # stop for the remainder of the sampling interval
                operation_time = time.time() - operation_start_time
                sample_delay = self.sample_interval_secs - operation_time
                if sample_delay < 0:
                    normalized_sample_delay = min(
                        operation_time, self.sample_interval_secs
                    )
                    log.warning(
                        f"Sample interval of {self.sample_interval_secs}s is too short, implying wait of {sample_delay:.2f}s. Resetting delay to {normalized_sample_delay:.2f}s."
                    )
                    # don't use 0: never spin
                    sample_delay = normalized_sample_delay
                log.debug(f"Waiting {sample_delay:.2f}s until the next sample.")
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
                log.debug(f"Loaded {len(output)} weather fields.")
            except JSONDecodeError:
                log.warning(f"JSON parse error of {r.content!r}", exc_info=True)
                return None
        except (OSError, ConnectionError, RequestException):
            log.warning("Problem getting weather data.", exc_info=True)
            return None
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(f"Fetching weather data using coordinates [{self.lat},{self.lon}].")
        with exception_handler(
            connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True
        ) as app_socket:
            while not threads.shutting_down:
                wd = self.get_weather_data()
                log.debug(f"Received weather data: {wd}")
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
                            f"Derived {sun_output}% sun output from {sunrise=},{date_value=},{sunset=},{midday_secs=},{secs_from_dark=}"
                        )
                    else:
                        log.debug(
                            f"Using {sun_output}% sun output from {sunrise=},{date_value=},{sunset=}"
                        )
                    country = wd["sys"]["country"]
                    weather["midday_pct"] = sun_output
                    log.debug(
                        f"{country}: Sending {len(weather)} fields for publication: {weather}"
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
        self._minimum_bms_count = app_config.getint("alert_thresholds", "minimum_bms_count")
        self.pd_count_dedup_key: str | None = None
        self.pd_count_alert_triggered = False

    @staticmethod
    def _extract_scalars(data: dict) -> tuple[dict, list[float]]:
        """Extract scalar battery metrics and cell voltage array.

        Returns (scalars_dict, cells_v_list).
        Scalar dict contains all single-value metrics suitable for a labeled time series.
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
        temp_c_values = [t.get("celsius") for t in temps if t.get("celsius") is not None]
        if len(temp_c_values) > 0:
            scalars["battery_temp_c"] = temp_c_values[0]
        if len(temp_c_values) > 1:
            scalars["mos_temp_c"] = temp_c_values[1]

        return scalars, cells_v

    # noinspection PyBroadException
    def run(self):
        log.info(f"Starting BMS reader on {self.port} at {self.baudrate} baud...")
        self.reader = SerialPortReader(port=self.port, baudrate=self.baudrate)

        if not self.reader.connect():
            log.error(f"Could not connect to BMS serial port {self.port}")
            return

        log.info(f"Connected to BMS on {self.port}.")
        # Frames are queued internally; we pull them from the main thread.
        self.reader.start(on_frame=None)

        # Initialize heartbeat timer so alert fires if no data arrives within 60s
        self.last_bms_data_time = time.time()
        self._startup_time = self.last_bms_data_time

        # Seed PagerDuty state so any previously-open incidents auto-resolve on first data
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

                # Block until a valid, decoded frame arrives (or timeout to check shutdown flag)
                data = self.reader.get_result(timeout=1.0)
                if data is None:
                    # Check for data-loss timeout and trigger PagerDuty if needed
                    if (
                        self.last_bms_data_time > 0
                        and time.time() - self.last_bms_data_time > BMS_DATA_LOSS_TIMEOUT
                        and not self.pd_alert_triggered
                    ):
                        if self.pd_client is not None:
                            try:
                                self.pd_dedup_key = self.pd_client.trigger(
                                    dedup_key="bms_heartbeat",
                                    summary=f"BMS serial data loss on {self.port} \u2014 no frames received for >{BMS_DATA_LOSS_TIMEOUT} seconds",
                                    source=str(DEVICE_NAME_BASE),
                                    severity="warning",
                                )
                                self.pd_alert_triggered = True
                                log.warning(
                                    "PagerDuty alert triggered for BMS data loss (dedup_key=%s).",
                                    self.pd_dedup_key,
                                )
                            except Exception:
                                log.warning("PagerDuty trigger failed.", exc_info=True)
                        else:
                            log.warning(
                                "PagerDuty client not configured; cannot trigger alert for BMS data loss."
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
                            log.warning("PagerDuty heartbeat resolve failed.", exc_info=True)
                    else:
                        log.warning(
                            "PagerDuty client not configured; cannot resolve alert for BMS data restoration."
                        )
                    # Also resolve count alert when data flow resumes
                    if self.pd_count_alert_triggered and self.pd_client is not None:
                        try:
                            if self.pd_count_dedup_key is not None:
                                self.pd_client.resolve(
                                    dedup_key=self.pd_count_dedup_key,
                                )
                            self.pd_count_alert_triggered = False
                            log.info("PagerDuty count incident resolved (data flow restored).")
                        except Exception:
                            log.warning("PagerDuty count resolve failed.", exc_info=True)

                # Log newly detected packs
                if addr not in seen_addresses:
                    seen_addresses.add(addr)
                    model_str = (
                        f" ({data.get('model', '')})"
                        if data.get("model")
                        else ""
                    )
                    serial_str = (
                        f" SN:{data.get('serial', '')}"
                        if data.get("serial")
                        else ""
                    )
                    log.info(
                        "[NEW] %s (#%02X) detected: %d cells, %.2fV%s%s",
                        bms_name,
                        addr,
                        bms_info.get("cell_count", 0),
                        bms_info.get("voltage_v", 0),
                        model_str,
                        serial_str,
                    )

                # Log per-frame summary
                log.debug(
                    "%s (#%02X): %d cells, %.2fV, min=%.3fV max=%.3fV diff=%.1fmV",
                    bms_name,
                    addr,
                    bms_info.get("cell_count", 0),
                    bms_info.get("voltage_v", 0),
                    bms_info.get("min_cell_v", 0),
                    bms_info.get("max_cell_v", 0),
                    bms_info.get("cell_diff_mv", 0),
                )

                # Cache latest data per address (keep for compatibility)
                self.bms_data[addr] = scalars

                # Send labeled battery-level scalars
                battery_metrics = [
                    {"labels": {"bms_addr": bms_name}, "metrics": scalars}
                ]
                log.debug("Sending %s frame for publication (%d scalars).", bms_name, len(scalars))
                app_socket.send_pyobj({"battery": battery_metrics})

                # Send per-cell voltage as a separate labeled point
                if cells_v:
                    cell_metrics = []
                    for idx, cell_v in enumerate(cells_v):
                        cell_metrics.append({
                            "labels": {"bms_addr": bms_name, "cell": f"{idx + 1:02d}"},
                            "metrics": {"voltage_v": cell_v},
                        })
                    app_socket.send_pyobj({"bms_cell": cell_metrics})

                # Check if enough distinct BMS units have reported recently
                now = time.time()
                active_addrs = sum(
                    1 for ts in self._bms_last_seen.values()
                    if now - ts < BMS_DATA_LOSS_TIMEOUT
                )
                if (active_addrs < self._minimum_bms_count
                        and not self.pd_count_alert_triggered
                        and time.time() - self._startup_time >= 300):
                    if self.pd_client is not None:
                        try:
                            self.pd_count_dedup_key = self.pd_client.trigger(
                                dedup_key="bms_count",
                                summary=f"Only {active_addrs}/{self._minimum_bms_count} BMS units reporting on {self.port}",
                                source=str(DEVICE_NAME_BASE),
                                severity="warning",
                            )
                            self.pd_count_alert_triggered = True
                            log.warning(
                                "PagerDuty alert triggered for low BMS count (dedup_key=%s).",
                                self.pd_count_dedup_key,
                            )
                        except Exception:
                            log.warning("PagerDuty count trigger failed.", exc_info=True)
                    else:
                        log.warning(
                            "PagerDuty client not configured; cannot trigger alert for low BMS count."
                        )
                elif active_addrs >= self._minimum_bms_count and self.pd_count_alert_triggered:
                    if self.pd_client is not None:
                        try:
                            if self.pd_count_dedup_key is not None:
                                self.pd_client.resolve(
                                    dedup_key=self.pd_count_dedup_key,
                                )
                            self.pd_count_alert_triggered = False
                            log.info("PagerDuty count incident resolved.")
                        except Exception:
                            log.warning("PagerDuty count resolve failed.", exc_info=True)
                    else:
                        log.warning(
                            "PagerDuty client not configured; cannot resolve alert for BMS count restoration."
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
        log.info(f"Subscribing to topic [{subscription_topic}]...")
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
        log.debug(f"{topic} received {len(payload)} bytes.")
        msg_data = None
        try:
            log.debug(f"{topic} received: {payload}")
            msg_data = json.loads(payload)
        except JSONDecodeError:
            log.exception(f"Unstructured message: {payload}")
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
                    log.info(f"[{switch_bank}] Switch {ids + 1} is now in state [{s}]")
            # state capture
            self._switch_state[switch_bank] = new_state

    def set_switch_state(self, switch_state=1):
        for switch_bank in self._switch_state.keys():
            if switch_bank not in self._mqtt_switch_devices:
                log.warning(
                    f"Not changing state for {switch_bank} due to missing configuration."
                )
                continue
            mqtt_pub_topic = "/".join(
                [f"{self._mqtt_subscribe_topic_prefix}", "control", switch_bank]
            )
            mqtt_update = list()
            for _ids, _ in enumerate(self._switch_state[switch_bank]):
                mqtt_update.append(switch_state)
            message_data = json.dumps({"state": mqtt_update})
            log.debug(
                f"[{mqtt_pub_topic}] Publishing {len(message_data)} bytes: [{message_data}]"
            )
            if self._mqtt_client is not None:
                self._mqtt_client.publish(topic=mqtt_pub_topic, payload=message_data)

    def get_power_generation_avg(self, value):
        self._power_generation_history.append(value)
        total: float = 0.0
        for sample in self._power_generation_history:
            total += sample
        return total / len(self._power_generation_history)

    # noinspection PyBroadException
    def run(self):
        log.info(f"Connecting to MQTT server {self._mqtt_server_address}...")
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
            prev_switch_state = 0
            while not threads.shutting_down:
                switch_stats = dict()
                rc = self._mqtt_client.loop()
                if rc == MQTT_ERR_NO_CONN or self._disconnected:
                    raise ResourceWarning(
                        f"No connection to MQTT broker at {self._mqtt_server_address} (disconnected? {self._disconnected})"
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
                # check 1: calculate surplus as a function of PV reported *usage* and how much the batteries are supplying
                pv1_power_w = float(inverter_data["pv1_power_w"])
                pv2_power_w = float(inverter_data["pv2_power_w"])
                battery_power_w = float(inverter_data["battery_power_w"])
                power_generation_w_avg = self.get_power_generation_avg(
                    value=pv1_power_w + pv2_power_w - battery_power_w
                )
                # disable switch if battery is critically low without adequate surplus (i.e. not charging from solar)
                battery_soc_pct = inverter_data["battery_soc_pct"]
                if (
                    battery_soc_pct < BATTERY_CRITICAL_PCT
                    and power_generation_w_avg < 0
                ):
                    switch_state = 0
                    switch_stats["surplus_ration"] = 1
                # check 2: determine battery state of charge and whether there is any grid fallback
                grid_voltage_l1_v = float(inverter_data["grid_voltage_l1_v"])
                grid_voltage_l2_v = float(inverter_data["grid_voltage_l2_v"])
                grid_voltage = max(grid_voltage_l1_v, grid_voltage_l2_v)
                # more conservative rationing if there is no grid backup (draw assumes no surplus)
                if (
                    battery_soc_pct < BATTERY_LOW_PCT
                    and grid_voltage < 90
                    and battery_power_w >= BATTERY_MAJOR_DRAW_W
                ):
                    switch_state = 0
                    switch_stats["battery_ration"] = 1
                # check 3: determine whether the inverter is no longer pulling from solar or battery (i.e. from grid)
                inverter_l1_power_w = float(inverter_data["inverter_l1_power_w"])
                inverter_l2_power_w = float(inverter_data["inverter_l2_power_w"])
                # can't use min/max because l2 is normally 0
                inverter_power_w = inverter_l1_power_w + inverter_l2_power_w
                if inverter_power_w < 0:
                    switch_state = 0
                    switch_stats["battery_ration"] = 1
                # log the supporting data
                log_msg = (
                    f"Inverter is delivering {inverter_power_w}w to consumers from backup (solar/battery). "
                    f"Power surplus average is {power_generation_w_avg:.2f}w ({pv1_power_w=:.2f}w, {pv2_power_w=:.2f}w, {battery_power_w=:.2f}w). "
                    f"Battery discharge {battery_power_w}w with remaining charge of {battery_soc_pct}% and supporting grid voltage of {grid_voltage}v. "
                    f"Updating switch banks to [{switch_state}]."
                )
                if prev_switch_state != switch_state:
                    log.info(log_msg)
                elif log.level == logging.DEBUG:
                    log.debug(log_msg)
                # update switches
                self.set_switch_state(switch_state=switch_state)
                prev_switch_state = switch_state
                # post stats
                switch_stats["switch_state"] = switch_state
                app_socket.send_pyobj({"switches": switch_stats})
                # for other interested consumers
                if self._mqtt_client is not None:
                    self._mqtt_client.publish(
                        topic="inverter/state", payload=json.dumps(inverter_data)
                    )
        self.close()


class EventProcessor(AppThread, Closable):
    def __init__(self, debug_metrics, influx_client=None):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_APP)

        self.debug_metrics = debug_metrics

        self.influxdb_rw = None
        self.influxdb_ro = None
        if influx_client is not None:
            self.influxdb_bucket = app_config.get("influxdb", "bucket")
            self.influxdb_rw = influx_client.write_api(write_options=ASYNCHRONOUS)
            self.influxdb_ro = influx_client.query_api()

    def _influxdb_write(self, point_name, field_name, field_value, extra_tags=None):
        if self.influxdb_rw is not None:
            try:
                point = Point(point_name).tag("application", APP_NAME).tag("device", DEVICE_NAME_BASE)
                if extra_tags:
                    for tag_key, tag_value in extra_tags.items():
                        point = point.tag(tag_key, str(tag_value))
                self.influxdb_rw.write(
                    bucket=self.influxdb_bucket,
                    record=point.field(field_name, field_value),
                )
            except Exception:
                log.warning("Unable to post to InfluxDB.", exc_info=True)

    # noinspection PyBroadException
    def run(self):
        log.debug(f"Debug metrics are {self.debug_metrics}.")
        my_socket = self.get_socket()
        gauges = {}
        with exception_handler(
            connect_url=URL_WORKER_MQTT_PUBLISH, and_raise=False, shutdown_on_error=True
        ) as mqtt_socket:
            while not threads.shutting_down:
                event = my_socket.recv_pyobj()
                log.debug(event)
                if isinstance(event, dict):
                    for point_name in list(event):
                        point_items = event[point_name]
                        if isinstance(point_items, list):
                            # Labeled format: list of {"labels": {...}, "metrics": {...}}
                            for entry in point_items:
                                labels = entry.get("labels", {})
                                metrics = entry.get("metrics", {})
                                if point_name in self.debug_metrics:
                                    log.debug(f"Log-only Metric: {point_name} labels={labels} metrics={metrics}")
                                    continue
                                for metric_key, metric_value in metrics.items():
                                    if not isinstance(metric_value, (int, float, bool)):
                                        continue
                                    self._influxdb_write(point_name, metric_key, metric_value, extra_tags=labels)
                                    gauge_key = f"{point_name}_{metric_key}"
                                    if gauge_key not in gauges:
                                        gauges[gauge_key] = Gauge(
                                            name=gauge_key,
                                            documentation=f"{point_name} {metric_key}",
                                            labelnames=list(labels.keys()),
                                        )
                                    label_values = [str(labels[k]) for k in gauges[gauge_key]._labelnames]
                                    try:
                                        if len(label_values) > 0:
                                            log.debug(f"Setting gauge {gauge_key} with labels {label_values} to value {metric_value}.")
                                            gauges[gauge_key].labels(*label_values).set(metric_value)
                                        else:
                                            gauges[gauge_key].set(metric_value)
                                    except ValueError as e:
                                        log.warning(
                                            f"Invalid value for gauge {gauge_key} with label values {label_values}: {metric_value} ({e})"
                                        )
                        elif isinstance(point_items, dict):
                            # Legacy flat format
                            for key, value in point_items.items():
                                if point_name in self.debug_metrics:
                                    log.debug(
                                        f"Log-only Metric: {point_name}.{key} = {value}"
                                    )
                                    continue
                                self._influxdb_write(point_name, key, value)
                                gauge_name = key
                                if key not in gauges:
                                    if not gauge_name.startswith(point_name):
                                        gauge_name = f"{point_name}_{key}"
                                    gauges[key] = Gauge(
                                        name=gauge_name, documentation=f"{point_name} {key}"
                                    )
                                try:
                                    gauges[key].set(value)
                                except ValueError as e:
                                    log.warning(
                                        f"Invalid value for gauge {gauge_name}: {value} ({e})"
                                    )
                        else:
                            log.warning(f"Unexpected point_items type for {point_name}: {type(point_items)}")
                        if point_name == "inverter" and point_name not in debug_metrics:
                            mqtt_socket.send_pyobj(point_items)
        self.close()


def main():
    global creds
    global debug_metrics
    creds = Creds()
    creds.validate_creds()
    # sentry instrumentation
    log.info("Loading Sentry.io instrumentation...")
    sentry_sdk.init(
        dsn=creds.get_creds(f"Sentry/{APP_NAME}/dsn"),
        integrations=[
            AsyncioIntegration(),
            SysExitIntegration(capture_successful_exits=True),
            ThreadingIntegration(propagate_scope=True),
        ],
        send_default_pii=True,
    )
    # load basic configuration
    app_path = Path(os.path.abspath(os.path.dirname(__file__))).parent
    mappings = None
    mappings_file = os.path.join(app_path, "config", "field_mappings.txt")
    with open(mappings_file) as mapping_file:
        try:
            mappings = json.loads(mapping_file.read())
            log.info(f"Loaded {len(mappings)} field mappings from {mappings_file}")
        except JSONDecodeError as e:
            log.exception(f"Error loading {mappings_file}.")
            raise e
    # load time series clients
    influx_client = None
    if is_flag_enabled("local-influxdb"):
        influxdb_url = creds.get_creds("InfluxDB/local/url")
        log.info(
            f"Connecting to InfluxDB at {influxdb_url}."
        )
        influx_client = InfluxDBClient(
            url=influxdb_url,
            token=creds.get_creds("InfluxDB/local/token"),
            org=creds.get_creds("InfluxDB/local/org"),
        )
    else:
        log.debug(
            'Not writing to InfluxDB due to feature flag "local-influxdb" being disabled.'
        )
    # ensure proper signal handling; must be main thread
    signal_handler = SignalHandler()
    event_processor = EventProcessor(debug_metrics=debug_metrics, influx_client=influx_client)
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
    nanny = threading.Thread(
        name="nanny", target=thread_nanny, args=(signal_handler,), daemon=True
    )
    # startup completed
    try:
        metric_port = app_config.getint("metrics", "network_port")
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            # this will produce multiple instances per process
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            log.info(f"Starting multi-process metric server on port {metric_port}...")
            start_http_server(metric_port, registry=registry)
        else:
            log.info(f"Starting metric server on port {metric_port}...")
            start_http_server(metric_port)
        log.info(f"Starting {APP_NAME} threads...")
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
        # start thread nanny
        nanny.start()
        log.info("Startup complete.")
        # hang around until something goes wrong
        threads.interruptable_sleep.wait()
        raise RuntimeWarning("Shutting down...")
    except (KeyboardInterrupt, RuntimeWarning, ContextTerminated):
        die()
    finally:
        zmq_term()
    bye()


if __name__ == "__main__":
    main()
