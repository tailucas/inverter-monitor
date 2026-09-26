#!/usr/bin/env python
import binascii
import os
import re
import socket
import threading
import time
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
from tailucas_pylib.zmq import URL_WORKER_APP, Closable, try_close, zmq_socket, zmq_term
from zmq.error import ContextTerminated, ZMQError

from app.load_alerts import CooldownLatch, LoadAlertEvaluator, TimeWindowAverage
from app.metrics import configure as metrics_configure
from app.serial_reader import SerialPortReader
from app.single_flight import SingleFlight
from app.telegram_bot import URL_WORKER_TELEGRAM  # noqa: E402

creds: Creds | None = None
debug_metrics = app_config.get("metrics", "debug_csv").split(",")

URL_WORKER_MQTT_PUBLISH = "inproc://mqtt-publish"
URL_WORKER_LOAD_MONITOR = "inproc://load-monitor"

# Points that carry notifications only and are never exported as metrics
NOTIFICATION_ONLY_POINTS = {"switch_event", "load_alert"}
# Points forwarded to the Telegram bot fan-out
TELEGRAM_FANOUT_POINTS = {"battery", "weather", "switch_event", "load_alert"}

# inverter polling: poll quickly and back off exponentially after failures
DEFAULT_POLL_BACKOFF_SECONDS = 1.0
MAX_POLL_BACKOFF_SECONDS = 60.0
# generous time bound for detecting implausible SoC steps between polls
IMPLAUSIBLE_SOC_WINDOW_SECONDS = 120
# weather: 60 s poll cadence with a bounded HTTP request timeout and
# exponential backoff (up to 600 s) when a fetch fails
DEFAULT_WEATHER_POLL_INTERVAL_SECONDS = 60
MAX_WEATHER_BACKOFF_SECONDS = 600
WEATHER_REQUEST_TIMEOUT_SECONDS = 10
# rolling window for the surplus generation average
DEFAULT_GENERATION_AVERAGE_SECONDS = 300
# overcast switch reason: 100 % cloud with a self-extending cooldown
OVERCAST_CLOUDINESS_PCT = 100
DEFAULT_OVERCAST_COOLDOWN_SECONDS = 3600
# cloudiness older than this is treated as unknown for overcast rationing
CLOUDINESS_STALE_SECONDS = 180
IMPLAUSIBLE_CHANGE_PERCENTAGE = 5
BATTERY_LOW_PCT = 45
# assuming CFE drop-out at 30%
BATTERY_CRITICAL_PCT = 40
# idle small home ~ 300W
BATTERY_MAJOR_DRAW_W = 500
# BMS serial data loss timeout
BMS_DATA_LOSS_TIMEOUT = 600
# overall-load alerting defaults (optional [alert_thresholds] overrides)
DEFAULT_LOAD_WARNING_W = 7000
DEFAULT_LOAD_CRITICAL_W = 7500
DEFAULT_LOAD_CRITICAL_RESOLVE_SECONDS = 60
DEFAULT_LOAD_SHED_COOLDOWN_SECONDS = 600
# PagerDuty dedup key for the high-load incident class
PD_LOAD_DEDUP_KEY = "load_high"

# OpenTelemetry meter and tracer (module-level, shared across all threads)
OTEL_METER = metrics.get_meter(APP_NAME)
OTEL_TRACER = trace.get_tracer(APP_NAME)
# Timing histograms for key operations (module-level, thread-safe instruments)
INVERTER_QUERY_DURATION = OTEL_METER.create_gauge(
    name="inverter_query_duration_seconds",
    description="Time to query inverter (TCP connect+send+recv+parse)",
)
INVERTER_CYCLE_DURATION = OTEL_METER.create_gauge(
    name="inverter_cycle_duration_seconds",
    description="Full inverter sampling cycle (query + plausibility retries + publish)",
)
INVERTER_POLL_BACKOFF = OTEL_METER.create_gauge(
    name="inverter_poll_backoff_seconds",
    description="Current inverter poll backoff after the last poll outcome",
)
WEATHER_FETCH_DURATION = OTEL_METER.create_gauge(
    name="weather_fetch_duration_seconds",
    description="Round-trip time for OpenWeather API fetch",
)
BMS_FRAME_PROCESS_DURATION = OTEL_METER.create_gauge(
    name="bms_frame_process_duration_seconds",
    description="Time from BMS frame receipt to ZMQ publish",
)
MQTT_PUBLISH_DURATION = OTEL_METER.create_gauge(
    name="mqtt_publish_duration_seconds",
    description="Time for traceparent injection + client.publish",
)
EVENT_PROCESS_DURATION = OTEL_METER.create_gauge(
    name="event_process_duration_seconds",
    description="Time for one event (InfluxDB + OTEL gauge + fan-out)",
)


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


def numeric_field(value: object) -> float | None:
    """Coerce a telemetry value to a rounded float, or None if unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2)


def inverter_supporting_fields(inverter_data: dict) -> dict:
    """Extract load/battery/PV/grid values for alert detail payloads."""
    pv1 = numeric_field(inverter_data.get("pv1_power_w"))
    pv2 = numeric_field(inverter_data.get("pv2_power_w"))
    l1 = numeric_field(inverter_data.get("inverter_l1_power_w"))
    l2 = numeric_field(inverter_data.get("inverter_l2_power_w"))
    g1 = numeric_field(inverter_data.get("grid_voltage_l1_v"))
    g2 = numeric_field(inverter_data.get("grid_voltage_l2_v"))
    return {
        "load_w": numeric_field(inverter_data.get("total_load_power_w")),
        "battery_soc_pct": numeric_field(inverter_data.get("battery_soc_pct")),
        "battery_power_w": numeric_field(inverter_data.get("battery_power_w")),
        "pv_power_w": (
            round(pv1 + pv2, 2) if pv1 is not None and pv2 is not None else None
        ),
        "inverter_power_w": (
            round(l1 + l2, 2) if l1 is not None and l2 is not None else None
        ),
        "grid_voltage_v": (
            round(max(g1, g2), 2) if g1 is not None and g2 is not None else None
        ),
    }


class LoggerReader(AppThread):
    def __init__(
        self,
        field_mappings,
        logger_sn,
        logger_ip,
        logger_port,
        poll_backoff_seconds=DEFAULT_POLL_BACKOFF_SECONDS,
        max_poll_backoff_seconds=MAX_POLL_BACKOFF_SECONDS,
    ):
        AppThread.__init__(self, name=self.__class__.__name__)
        self.field_mappings = field_mappings
        self.logger_sn = logger_sn
        self.logger_ip = logger_ip
        self.logger_port = logger_port
        self.poll_backoff_seconds = poll_backoff_seconds
        self.max_poll_backoff_seconds = max_poll_backoff_seconds
        self._poll_backoff = poll_backoff_seconds
        self._query_flight = SingleFlight()

    def _log_context(self) -> dict:
        """Structured context shared by inverter fetch failure logs."""
        return {
            "logger_ip": self.logger_ip,
            "logger_port": self.logger_port,
            "logger_sn": self.logger_sn,
        }

    def get_logger_data(self):
        """Query the inverter, timing every attempt (success or failure)."""
        _query_start = time.time()
        try:
            return self._read_logger_data()
        except Exception:
            # every fetch failure must be visible with logger context
            log.warning(
                "Unexpected error while querying inverter",
                exc_info=True,
                extra=self._log_context(),
            )
            return None
        finally:
            INVERTER_QUERY_DURATION.set(time.time() - _query_start)

    def _read_logger_data(self):
        _query_start = time.time()
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

            logger_sn_hex = f"{self.logger_sn:08x}"
            inverter_sn2 = bytearray.fromhex(
                logger_sn_hex[6:8]
                + logger_sn_hex[4:6]
                + logger_sn_hex[2:4]
                + logger_sn_hex[0:2]
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
            try:
                address_info = socket.getaddrinfo(
                    self.logger_ip,
                    self.logger_port,
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                )
            except OSError as msg:
                log.warning(
                    "Unable to resolve inverter logger address",
                    extra={**self._log_context(), "error": str(msg)},
                )
                return None
            for res in address_info:
                family, socktype, proto, canonname, sockadress = res
                try:
                    client_socket = socket.socket(family, socktype, proto)
                    client_socket.settimeout(10)
                    client_socket.connect(sockadress)
                except OSError as msg:
                    log.warning(
                        "Socket connect error",
                        extra={**self._log_context(), "error": str(msg)},
                    )
                    return None

            if client_socket is None:
                log.warning(
                    "No usable socket address for inverter logger",
                    extra=self._log_context(),
                )
                return None

            # SEND DATA
            log.debug(
                "Sending data frame",
                extra={"frame_bytes": len(frame_bytes), "chunk_number": chunks},
            )
            try:
                client_socket.sendall(frame_bytes)
            except OSError as msg:
                log.warning(
                    "Socket send error",
                    extra={
                        **self._log_context(),
                        "chunk_number": chunks,
                        "frame_bytes": len(frame_bytes),
                        "error": str(msg),
                    },
                )
                try:
                    client_socket.close()
                except OSError:
                    log.debug("Ignoring socket close error", exc_info=True)
                return None

            # RECEIVE RESPONSE
            data = None
            try:
                data = client_socket.recv(1024)
            except TimeoutError as msg:
                log.warning(
                    "Socket receive timeout",
                    extra={
                        **self._log_context(),
                        "chunk_number": chunks,
                        "error": str(msg),
                    },
                )
                return None
            except OSError as msg:
                log.warning(
                    "Socket receive error",
                    extra={
                        **self._log_context(),
                        "chunk_number": chunks,
                        "error": str(msg),
                    },
                )
                return None
            finally:
                try:
                    client_socket.close()
                except OSError as msg:
                    log.warning(
                        "Socket close error",
                        extra={
                            **self._log_context(),
                            "chunk_number": chunks,
                            "error": str(msg),
                        },
                    )
            if not data:
                log.warning(
                    "Empty response from inverter logger",
                    extra={
                        **self._log_context(),
                        "chunk_number": chunks,
                        "response_bytes": 0 if data is None else len(data),
                    },
                )
                return None

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
                        extra={
                            **self._log_context(),
                            "chunk_number": chunks,
                            "register": "0x" + str(hex(a + pini)[2:].zfill(4)).upper(),
                            "response_bytes": len(data),
                            "response_hex": data[:64].hex(),
                        },
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
        _query_duration = time.time() - _query_start
        log.debug(
            "Fetched fields",
            extra={
                "field_count": len(output),
                "chunk_count": chunks,
                "query_duration_secs": round(_query_duration, 3),
            },
        )
        return output

    def query_now(self) -> dict | None:
        """Trigger a real-time inverter query, or wait for an in-flight one.

        Uses SingleFlight to serialise access with the scheduled polling
        loop so that concurrent queries never reach the logger socket.
        Returns the parsed field dict, or None on failure.
        """
        return self._query_flight.call(self.get_logger_data)

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
                now = operation_start_time
                try:
                    logger_data = self.query_now()
                except Exception:
                    log.warning(
                        "Inverter query raised an unexpected error",
                        exc_info=True,
                        extra=self._log_context(),
                    )
                    logger_data = None
                accepted = False
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
                        elif prev_battery_soc is None:
                            prev_battery_soc = battery_soc
                            prev_battery_soc_set = now
                            accepted = True
                        else:
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
                            # check for an implausible change within a time bound
                            if (
                                abs(soc_delta_pct) >= IMPLAUSIBLE_CHANGE_PERCENTAGE
                                and prev_battery_soc_last_set
                                < IMPLAUSIBLE_SOC_WINDOW_SECONDS
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
                                accepted = True
                if accepted and logger_data is not None and len(logger_data) > 0:
                    log.debug(
                        "Sending fields for publication",
                        extra={"field_count": len(logger_data)},
                    )
                    app_socket.send_pyobj({"inverter": logger_data})
                    poll_delay = self.poll_backoff_seconds
                    self._poll_backoff = self.poll_backoff_seconds
                else:
                    # exponential back-off on failed or implausible polls
                    poll_delay = self._poll_backoff
                    self._poll_backoff = min(
                        poll_delay * 2, self.max_poll_backoff_seconds
                    )
                    log.warning(
                        "Inverter poll failed; backing off",
                        extra={
                            "poll_backoff_secs": round(poll_delay, 2),
                            "next_backoff_secs": round(self._poll_backoff, 2),
                            "max_backoff_secs": self.max_poll_backoff_seconds,
                        },
                    )
                operation_time = time.time() - operation_start_time
                INVERTER_CYCLE_DURATION.set(operation_time)
                INVERTER_POLL_BACKOFF.set(poll_delay)
                log.debug(
                    "Waiting before the next poll",
                    extra={
                        "poll_delay_secs": round(poll_delay, 2),
                        "cycle_secs": round(operation_time, 2),
                    },
                )
                threads.interruptable_sleep.wait(poll_delay)


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
        self.poll_interval_seconds = app_config.getint(
            "weather",
            "poll_interval_seconds",
            fallback=DEFAULT_WEATHER_POLL_INTERVAL_SECONDS,
        )
        self.max_backoff_seconds = MAX_WEATHER_BACKOFF_SECONDS
        self._poll_backoff = self.poll_interval_seconds

    def get_weather_data(self):
        _weather_start = time.time()
        output = None
        try:
            r = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "lat": self.lat,
                    "lon": self.lon,
                    "appid": self.api_key,
                },
                timeout=WEATHER_REQUEST_TIMEOUT_SECONDS,
            )
            try:
                output = json.loads(r.content)
                log.debug("Loaded weather fields", extra={"field_count": len(output)})
            except JSONDecodeError:
                log.warning(
                    "JSON parse error of weather response",
                    exc_info=True,
                    extra={
                        "lat": self.lat,
                        "lon": self.lon,
                        "response_content": repr(r.content)[:512],
                    },
                )
                _weather_duration = time.time() - _weather_start
                WEATHER_FETCH_DURATION.set(_weather_duration)
                return None
        except OSError, ConnectionError, RequestException:
            log.warning(
                "Problem getting weather data.",
                exc_info=True,
                extra={
                    "lat": self.lat,
                    "lon": self.lon,
                    "timeout_secs": WEATHER_REQUEST_TIMEOUT_SECONDS,
                },
            )
            _weather_duration = time.time() - _weather_start
            WEATHER_FETCH_DURATION.set(_weather_duration)
            return None
        _weather_duration = time.time() - _weather_start
        WEATHER_FETCH_DURATION.set(_weather_duration)
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
                weather = None
                if wd is not None and len(wd) > 0:
                    try:
                        weather = self._derive_weather(wd)
                    except Exception:
                        log.warning(
                            "Unexpected error processing weather data",
                            exc_info=True,
                            extra={
                                "lat": self.lat,
                                "lon": self.lon,
                                "weather_keys": sorted(wd.keys())
                                if isinstance(wd, dict)
                                else str(type(wd)),
                            },
                        )
                if weather is not None:
                    log.debug(
                        "Sending weather fields for publication",
                        extra={"field_count": len(weather), "weather": weather},
                    )
                    app_socket.send_pyobj({"weather": weather})
                    poll_delay = self.poll_interval_seconds
                    self._poll_backoff = self.poll_interval_seconds
                else:
                    # exponential back-off on failed or malformed fetches
                    poll_delay = self._poll_backoff
                    self._poll_backoff = min(poll_delay * 2, self.max_backoff_seconds)
                    log.warning(
                        "Weather fetch failed; backing off",
                        extra={
                            "poll_backoff_secs": round(poll_delay, 2),
                            "next_backoff_secs": round(self._poll_backoff, 2),
                            "max_backoff_secs": self.max_backoff_seconds,
                        },
                    )
                log.debug(
                    "Waiting before the next weather poll",
                    extra={"poll_delay_secs": round(poll_delay, 2)},
                )
                threads.interruptable_sleep.wait(poll_delay)

    def _derive_weather(self, wd: dict) -> dict:
        """Derive the published weather fields from an OpenWeather payload."""
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
        weather["midday_pct"] = sun_output
        log.debug(
            "Derived weather fields",
            extra={
                "country": wd["sys"]["country"],
                "field_count": len(weather),
                "weather": weather,
            },
        )
        return weather


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
                                log.info(
                                    "PagerDuty alert triggered for BMS data loss",
                                    extra={"dedup_key": self.pd_dedup_key},
                                )
                            except Exception:
                                log.info("PagerDuty trigger failed.", exc_info=True)
                        else:
                            log.warning(
                                "PagerDuty not configured; cannot trigger"
                                " alert for BMS data loss."
                            )
                    continue

                addr = data.get("addr")
                if addr is None:
                    continue
                _bms_process_start = time.time()

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
                            log.info(
                                "PagerDuty heartbeat incident resolved.",
                                extra={"dedup_key": self.pd_dedup_key},
                            )
                        except Exception:
                            log.info(
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
                                extra={"dedup_key": self.pd_count_dedup_key},
                            )
                        except Exception:
                            log.info("PagerDuty count resolve failed.", exc_info=True)

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

                _bms_process_duration = time.time() - _bms_process_start
                BMS_FRAME_PROCESS_DURATION.set(_bms_process_duration)

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
                            log.info(
                                "PagerDuty alert triggered for low BMS count",
                                extra={"dedup_key": self.pd_count_dedup_key},
                            )
                        except Exception:
                            log.info("PagerDuty count trigger failed.", exc_info=True)
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
                            log.info(
                                "PagerDuty count incident resolved.",
                                extra={"dedup_key": self.pd_count_dedup_key},
                            )
                        except Exception:
                            log.info("PagerDuty count resolve failed.", exc_info=True)
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

        generation_average_secs = app_config.getint(
            "alert_thresholds",
            "generation_average_seconds",
            fallback=DEFAULT_GENERATION_AVERAGE_SECONDS,
        )
        self._generation_average = TimeWindowAverage(
            window_secs=generation_average_secs
        )
        overcast_cooldown_secs = app_config.getint(
            "alert_thresholds",
            "overcast_cooldown_seconds",
            fallback=DEFAULT_OVERCAST_COOLDOWN_SECONDS,
        )
        self._overcast_latch = CooldownLatch(
            threshold=OVERCAST_CLOUDINESS_PCT,
            cooldown_secs=overcast_cooldown_secs,
            inclusive=True,
        )
        self._cloudiness_pct: float | None = None
        self._cloudiness_set_at: float | None = None

        load_warning_w = app_config.getint(
            "alert_thresholds", "load_warning_w", fallback=DEFAULT_LOAD_WARNING_W
        )
        self._load_shed_w = app_config.getint(
            "alert_thresholds", "load_shed_w", fallback=load_warning_w
        )
        self._load_shed_latch = CooldownLatch(
            threshold=self._load_shed_w,
            cooldown_secs=app_config.getint(
                "alert_thresholds",
                "load_shed_cooldown_seconds",
                fallback=DEFAULT_LOAD_SHED_COOLDOWN_SECONDS,
            ),
        )

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
        """Set every configured bank and return the banks that changed."""
        changed_banks = []
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
            # only publish (and trace a switch event) on an actual state change
            if not any(s != switch_state for s in self._switch_state[switch_bank]):
                log.debug(
                    "Switch state unchanged; skipping control publish",
                    extra={
                        "switch_bank": switch_bank,
                        "switch_state": switch_state,
                        "current_state": self._switch_state[switch_bank],
                    },
                )
                continue
            message_data = json.dumps({"state": mqtt_update})
            _mqtt_publish_start = time.time()
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
                log.debug(
                    "MQTT message dispatched",
                    extra={
                        "topic": mqtt_pub_topic,
                        "message_bytes": len(message_data),
                        "traceparent": tp,
                    },
                )
                _mqtt_publish_duration = time.time() - _mqtt_publish_start
                MQTT_PUBLISH_DURATION.set(_mqtt_publish_duration)
            changed_banks.append(switch_bank)
        return changed_banks

    def _notify_switch_change(
        self,
        app_socket,
        changed_banks,
        switch_state,
        reason,
        inverter_data,
        now,
    ):
        """Fan out a switch-bank notification for the Telegram bot."""
        if not changed_banks:
            return
        event = {
            "switch_banks": list(changed_banks),
            "state": int(switch_state),
            "reason": reason,
            "timestamp": round(now, 3),
        }
        supporting = inverter_supporting_fields(inverter_data)
        event.update(
            {key: value for key, value in supporting.items() if value is not None}
        )
        app_socket.send_pyobj({"switch_event": event})
        log.info(
            "Switch bank notification queued",
            extra={
                "switch_banks": list(changed_banks),
                "switch_state": switch_state,
                "reason": reason,
            },
        )

    def get_power_generation_avg(self, value, now):
        return self._generation_average.add(value, now)

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
                if "cloudiness_pct" in inverter_data:
                    # weather fan-out feeding the overcast rationing reason
                    self._cloudiness_pct = numeric_field(
                        inverter_data.get("cloudiness_pct")
                    )
                    self._cloudiness_set_at = time.time()
                    log.debug(
                        "Weather sample received for overcast rationing",
                        extra={"cloudiness_pct": self._cloudiness_pct},
                    )
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
                switch_stats["load_shed"] = 0
                switch_stats["overcast"] = 0
                switch_stats["surplus_ration"] = 0
                switch_stats["battery_ration"] = 0
                now = time.time()
                load_field = inverter_data.get("total_load_power_w")
                if isinstance(load_field, bool) or not isinstance(
                    load_field, (int, float)
                ):
                    load_shed_active = self._load_shed_latch.active
                    log.debug(
                        "Load value missing; retaining load-shed state",
                        extra={
                            "load_value": repr(load_field),
                            "load_shed": int(load_shed_active),
                        },
                    )
                else:
                    load_shed_active = self._load_shed_latch.update(
                        float(load_field), now
                    )
                # check 0 (top priority): overall load shedding with cooldown
                if load_shed_active:
                    switch_state = 0
                    switch_stats["load_shed"] = 1
                # check 0b: overcast rationing (100 % cloudiness)
                cloudiness_age = None
                if self._cloudiness_set_at is not None:
                    cloudiness_age = now - self._cloudiness_set_at
                if (
                    self._cloudiness_pct is not None
                    and cloudiness_age is not None
                    and cloudiness_age <= CLOUDINESS_STALE_SECONDS
                ):
                    overcast_active = self._overcast_latch.update(
                        self._cloudiness_pct, now
                    )
                else:
                    overcast_active = self._overcast_latch.active
                    if self._cloudiness_pct is not None:
                        log.debug(
                            "Ignoring stale weather; retaining overcast state",
                            extra={
                                "cloudiness_pct": self._cloudiness_pct,
                                "cloudiness_age_secs": round(cloudiness_age or 0.0, 1),
                                "overcast": int(overcast_active),
                            },
                        )
                if overcast_active:
                    switch_state = 0
                    switch_stats["overcast"] = 1
                if (
                    int(inverter_data["alert"]) == 1
                    and not load_shed_active
                    and not overcast_active
                ):
                    # do not load shed during an alert condition; a latched
                    # high-load condition takes priority over this guard
                    changed_banks = self.set_switch_state()
                    self._notify_switch_change(
                        app_socket=app_socket,
                        changed_banks=changed_banks,
                        switch_state=1,
                        reason="alert_restore",
                        inverter_data=inverter_data,
                        now=now,
                    )
                    app_socket.send_pyobj({"switches": switch_stats})
                    continue
                # check 1: calculate surplus as a function of PV reported *usage*
                # and how much the batteries are supplying
                pv1_power_w = float(inverter_data["pv1_power_w"])
                pv2_power_w = float(inverter_data["pv2_power_w"])
                battery_power_w = float(inverter_data["battery_power_w"])
                power_generation_w_avg = self.get_power_generation_avg(
                    value=pv1_power_w + pv2_power_w - battery_power_w,
                    now=now,
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
                    "load_w": numeric_field(load_field),
                    "load_shed": switch_stats["load_shed"],
                    "cloudiness_pct": self._cloudiness_pct,
                    "overcast": switch_stats["overcast"],
                    "switch_state": switch_state,
                }
                log.debug(log_msg, extra=log_fields)
                reason = "all_clear"
                if switch_stats["load_shed"]:
                    reason = "load_shed"
                elif switch_stats["overcast"]:
                    reason = "overcast"
                elif switch_stats["surplus_ration"]:
                    reason = "surplus_ration"
                elif switch_stats["battery_ration"]:
                    reason = "battery_ration"
                # update switches
                changed_banks = self.set_switch_state(switch_state=switch_state)
                self._notify_switch_change(
                    app_socket=app_socket,
                    changed_banks=changed_banks,
                    switch_state=switch_state,
                    reason=reason,
                    inverter_data=inverter_data,
                    now=now,
                )
                # post stats
                switch_stats["switch_state"] = switch_state
                app_socket.send_pyobj({"switches": switch_stats})
                # for other interested consumers
                if self._mqtt_client is not None:
                    _mqtt_publish_start = time.time()
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
                        log.debug(
                            "MQTT message dispatched",
                            extra={
                                "topic": "inverter/state",
                                "traceparent": tp,
                                "message_bytes": len(payload),
                            },
                        )
                        _mqtt_publish_duration = time.time() - _mqtt_publish_start
                        MQTT_PUBLISH_DURATION.set(_mqtt_publish_duration)
        self.close()


class LoadAlertMonitor(AppThread, Closable):
    """Evaluate overall-load samples for Telegram warnings and PagerDuty.

    Consumes every inverter sample fanned out by EventProcessor and:
    - warns over Telegram once per upward crossing of the warning threshold
      and confirms recovery once the load has held below it for the shed
      cooldown (the same clock as the switch load-shed release);
    - triggers a PagerDuty incident on a single sample above the critical
      threshold and resolves it after the load has held below it for the
      resolve window.
    """

    def __init__(self, telegram_enabled=False):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_LOAD_MONITOR)
        self._telegram_enabled = telegram_enabled

        self._load_warning_w = app_config.getint(
            "alert_thresholds", "load_warning_w", fallback=DEFAULT_LOAD_WARNING_W
        )
        self._load_critical_w = app_config.getint(
            "alert_thresholds", "load_critical_w", fallback=DEFAULT_LOAD_CRITICAL_W
        )
        self._load_shed_cooldown_secs = app_config.getint(
            "alert_thresholds",
            "load_shed_cooldown_seconds",
            fallback=DEFAULT_LOAD_SHED_COOLDOWN_SECONDS,
        )
        self._evaluator = LoadAlertEvaluator(
            warning_w=self._load_warning_w,
            critical_w=self._load_critical_w,
            resolve_secs=app_config.getint(
                "alert_thresholds",
                "load_critical_resolve_seconds",
                fallback=DEFAULT_LOAD_CRITICAL_RESOLVE_SECONDS,
            ),
            cooldown_secs=self._load_shed_cooldown_secs,
        )

        self.pd_client: EventsApiV2Client | None = None
        if app_config.getboolean("app", "paging_enabled"):
            if creds is None:
                raise RuntimeError("Credentials not initialized")
            self.pd_client = EventsApiV2Client(
                routing_key=creds.get_creds("PagerDuty.inverter-monitor/routing_key")
            )
        # Seed the dedup key so a stale incident from a previous run resolves
        self.pd_dedup_key: str | None = PD_LOAD_DEDUP_KEY

    def _notify_telegram(self, telegram_socket, alert):
        if telegram_socket is None:
            log.info(
                "Telegram bot disabled; load alert not sent",
                extra={"alert_kind": alert.get("kind")},
            )
            return
        telegram_socket.send_pyobj({"load_alert": alert})

    def _trigger_pd(self, load_w, details):
        if self.pd_client is None:
            # Configuration gap is logged at startup; keep the state clean
            self._evaluator.pd_trigger_succeeded()
            return
        try:
            self.pd_dedup_key = self.pd_client.trigger(
                dedup_key=PD_LOAD_DEDUP_KEY,
                summary=(
                    f"High load on {DEVICE_NAME_BASE}: {load_w:,.0f} W exceeds "
                    f"{self._load_critical_w} W"
                ),
                source=str(DEVICE_NAME_BASE),
                severity="warning",
                custom_details={
                    "load_w": round(load_w, 2),
                    "threshold_w": self._load_critical_w,
                    **{
                        key: value
                        for key, value in details.items()
                        if key != "timestamp"
                    },
                },
            )
            self._evaluator.pd_trigger_succeeded()
            log.info(
                "PagerDuty alert triggered for high load",
                extra={
                    "dedup_key": self.pd_dedup_key,
                    "load_w": round(load_w, 2),
                    "threshold_w": self._load_critical_w,
                },
            )
        except Exception:
            self._evaluator.pd_trigger_failed()
            log.info("PagerDuty load trigger failed; will retry.", exc_info=True)

    def _resolve_pd(self, load_w):
        if self.pd_client is None:
            self._evaluator.pd_resolve_succeeded()
            return
        try:
            if self.pd_dedup_key is not None:
                self.pd_client.resolve(dedup_key=self.pd_dedup_key)
            self._evaluator.pd_resolve_succeeded()
            log.info(
                "PagerDuty load incident resolved",
                extra={
                    "dedup_key": self.pd_dedup_key,
                    "load_w": round(load_w, 2),
                },
            )
        except Exception:
            self._evaluator.pd_resolve_failed()
            log.info("PagerDuty load resolve failed; will retry.", exc_info=True)

    # noinspection PyBroadException
    def run(self):
        log.info(
            "Starting load alert monitor",
            extra={
                "load_warning_w": self._load_warning_w,
                "load_critical_w": self._load_critical_w,
                "load_shed_cooldown_secs": self._load_shed_cooldown_secs,
                "paging_enabled": self.pd_client is not None,
                "telegram_enabled": self._telegram_enabled,
            },
        )
        if self.pd_client is None:
            log.warning(
                "PagerDuty not configured; high-load alerts will not page.",
                extra={"load_critical_w": self._load_critical_w},
            )
        telegram_socket = None
        if self._telegram_enabled:
            telegram_socket = zmq_socket(socket_type=zmq.PUSH)
            telegram_socket.connect(URL_WORKER_TELEGRAM)
        my_socket = self.get_socket()
        try:
            while not threads.shutting_down:
                try:
                    inverter_data = my_socket.recv_pyobj()
                except ContextTerminated, ZMQError:
                    break
                if not isinstance(inverter_data, dict):
                    continue
                load_field = inverter_data.get("total_load_power_w")
                if isinstance(load_field, bool) or not isinstance(
                    load_field, (int, float)
                ):
                    log.debug(
                        "Skipping load evaluation without a numeric load",
                        extra={"load_value": repr(load_field)},
                    )
                    continue
                load_w = float(load_field)
                now = time.time()
                decision = self._evaluator.evaluate(load_w, now)
                details = {
                    key: value
                    for key, value in inverter_supporting_fields(inverter_data).items()
                    if value is not None
                }
                details["timestamp"] = round(now, 3)
                if decision.warning:
                    log.info(
                        "Load exceeded warning threshold",
                        extra={
                            "load_w": round(load_w, 2),
                            "threshold_w": self._load_warning_w,
                            "cooldown_secs": self._load_shed_cooldown_secs,
                        },
                    )
                    self._notify_telegram(
                        telegram_socket,
                        {
                            "kind": "load_warning",
                            "load_w": round(load_w, 2),
                            "threshold_w": self._load_warning_w,
                            "cooldown_secs": self._load_shed_cooldown_secs,
                            **details,
                        },
                    )
                if decision.recovery:
                    log.info(
                        "Load recovered below warning threshold",
                        extra={
                            "load_w": round(load_w, 2),
                            "threshold_w": self._load_warning_w,
                            "cooldown_secs": self._load_shed_cooldown_secs,
                        },
                    )
                    self._notify_telegram(
                        telegram_socket,
                        {
                            "kind": "load_recovery",
                            "load_w": round(load_w, 2),
                            "threshold_w": self._load_warning_w,
                            "cooldown_secs": self._load_shed_cooldown_secs,
                            **details,
                        },
                    )
                if decision.pd_trigger:
                    self._trigger_pd(load_w, details)
                if decision.pd_resolve:
                    self._resolve_pd(load_w)
        finally:
            if telegram_socket is not None:
                try_close(telegram_socket)
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
            telegram_socket = zmq_socket(socket_type=zmq.PUSH)
            telegram_socket.connect(URL_WORKER_TELEGRAM)
            log.info("Telegram bot fan-out enabled")
        # Fan every inverter sample out to the load alert monitor
        load_socket = zmq_socket(socket_type=zmq.PUSH)
        load_socket.connect(URL_WORKER_LOAD_MONITOR)
        with exception_handler(
            connect_url=URL_WORKER_MQTT_PUBLISH, and_raise=False, shutdown_on_error=True
        ) as mqtt_socket:
            while not threads.shutting_down:
                event = my_socket.recv_pyobj()
                log.debug("Event received", extra={"event": event})
                _event_process_start = time.time()
                if isinstance(event, dict):
                    for point_name in list(event):
                        point_items = event[point_name]
                        if point_name in NOTIFICATION_ONLY_POINTS:
                            # notification payloads are forwarded, never gauged
                            pass
                        elif isinstance(point_items, list):
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
                        if point_name == "inverter":
                            # always fan out for load alert evaluation, even
                            # when the inverter point is a debug-only metric
                            load_socket.send_pyobj(point_items)
                            if point_name not in debug_metrics:
                                mqtt_socket.send_pyobj(point_items)
                        elif point_name == "weather":
                            # latest cloudiness feeds the overcast reason
                            mqtt_socket.send_pyobj(point_items)
                        # Forward telemetry and notifications to the Telegram bot
                        if (
                            telegram_socket is not None
                            and point_name in TELEGRAM_FANOUT_POINTS
                        ):
                            telegram_socket.send_pyobj({point_name: point_items})
                _event_process_duration = time.time() - _event_process_start
                EVENT_PROCESS_DURATION.set(_event_process_duration)
        if telegram_socket is not None:
            try_close(telegram_socket)
        try_close(load_socket)
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
    load_alert_monitor = LoadAlertMonitor(telegram_enabled=telegram_enabled)
    logger_reader: LoggerReader | None = None
    if app_config.getboolean("inverter", "logging_enabled"):
        logger_reader = LoggerReader(
            field_mappings=mappings,
            logger_sn=app_config.getint("inverter", "logger_sn"),
            logger_ip=app_config.get("inverter", "logger_address"),
            logger_port=app_config.getint("inverter", "logger_port"),
            poll_backoff_seconds=app_config.getfloat(
                "inverter",
                "poll_backoff_seconds",
                fallback=DEFAULT_POLL_BACKOFF_SECONDS,
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

        telegram_bot = TelegramBot(
            creds_obj=creds,
            inverter_query=logger_reader.query_now
            if logger_reader is not None
            else None,
        )
    else:
        log.warning("Telegram bot is disabled.")
    nanny = threading.Thread(
        name="nanny", target=thread_nanny, args=(signal_handler,), daemon=True
    )
    # startup completed
    try:
        log.info("Starting application threads", extra={"app_name": APP_NAME})
        event_processor.start()
        load_alert_monitor.start()
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
