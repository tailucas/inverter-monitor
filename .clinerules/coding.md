---
paths:
  - "app/**"
  - "tests/**"
  - "pyproject.toml"
---

# inverter-monitor Coding Standards

Multi-threaded energy monitoring application for Deye/Sunsynk hybrid inverters
and HinaESS Hi-5 BMS units: collects inverter telemetry over the logger's
proprietary TCP protocol, BMS telemetry over RS485 serial, correlates weather,
publishes to InfluxDB/MQTT, exports OpenTelemetry metrics, and drives load-shed
switch banks via MQTT.

## 1. Posture

- Hardware-facing code must be defensive: timeouts, retries, plausibility
  checks, and PagerDuty alerting for data loss are first-class concerns.
- Built on the `tailucas_pylib` framework (`AppThread`, `exception_handler`,
  `thread_nanny`, `die()`/`bye()` shutdown). Follow pylib's standards.

## 2. Application Architecture (`app/__main__.py`)

One `AppThread` per concern, wired over ZMQ inproc (`URL_WORKER_APP`,
`URL_WORKER_MQTT_PUBLISH`):

- `LoggerReader`: polls the inverter Wi-Fi logger (chunked binary protocol,
  CRC16-MODBUS validation via `libscrc`, field mappings from
  `config/field_mappings.txt`).
- `BmsReader`: consumes decoded BMS frames from `SerialPortReader`
  (`app/serial_reader.py`); assigns friendly BMS names, derives scalars,
  manages PagerDuty heartbeat/count incidents.
- `WeatherReader`: OpenWeather sampling correlated with inverter data.
- `MqttSubscriber`: consumes `inverter/state`, applies the rationing checks
  (surplus, battery SoC, grid fallback) and controls switch banks.
- `EventProcessor`: fans metrics out to InfluxDB + OTEL synchronous gauges.

Rules:

- New data sources get their own `AppThread` and register with the nanny.
- Blocking waits use `threads.interruptable_sleep`.
- Plausibility guards (implausible SoC deltas, zero-voltage outputs) must log
  the full supporting data as structured fields before discarding samples.

## 3. Serial & Protocol Code (`app/serial_reader.py`)

- `SerialPortReader` runs a background reader thread with an internal frame
  queue; callers pull via `get_result(timeout=...)`.
- The BMS decoder (`app/bms_decoder.py`) is pure logic and fully unit-tested
  (`tests/test_decoder.py`); keep it dependency-free and extend via tests.
- Unknown-but-valid frames get logged with raw hex fields for reverse
  engineering, not dropped silently.

## 4. Alerting & Metrics

- PagerDuty Events API V2: dedup keys are tracked per incident class
  (`bms_heartbeat`, `bms_count`); triggers and resolves must be logged with
  their dedup key as a structured field; seed alert state at startup so stale
  incidents auto-resolve.
- InfluxDB writes are feature-flagged (`local-influxdb`); OTEL synchronous
  gauges are named `<point_name>_<metric_key>` with attributes from the
  metrics payload's label set; log-only metrics are configured via
  `[metrics] debug_csv`.

## 5. Configuration

- All hardware endpoints/credentials come from `app.conf` sections
  (`inverter`, `bms`, `weather`, `mqtt`, `influxdb`, `metrics`,
  `alert_thresholds`) interpolated from `config/` at container start.
- Credentials via pylib `Creds` (1Password); never hardcode API keys.

## 6. Testing & Lint

- `uv run pytest tests/` must pass (decoder tests are the safety net for any
  protocol change).
- Ruff config selects F/E/W/B/I/UP without a custom line length; keep new
  code under 88 columns to avoid adding E501 noise (the file carries
  pre-existing long lines; do not grow that set).
- mypy runs with overrides for hardware/network client libraries.
