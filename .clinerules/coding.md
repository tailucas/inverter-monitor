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
`URL_WORKER_MQTT_PUBLISH`, `URL_WORKER_LOAD_MONITOR`, `URL_WORKER_TELEGRAM`):

- `LoggerReader`: polls the inverter Wi-Fi logger (chunked binary protocol,
  CRC16-MODBUS validation via `libscrc`, field mappings from
  `config/field_mappings.txt`). On-demand queries from the Telegram bot go
  through `query_now()` (`app/single_flight.py`) so ad-hoc and scheduled
  polls never hit the logger socket concurrently.
- `BmsReader`: consumes decoded BMS frames from `SerialPortReader`
  (`app/serial_reader.py`); assigns friendly BMS names, derives scalars,
  manages PagerDuty heartbeat/count incidents.
- `WeatherReader`: OpenWeather sampling correlated with inverter data.
- `MqttSubscriber`: consumes EventProcessor-forwarded inverter samples,
  subscribes to `{topic_prefix}/state/#`, applies the rationing checks
  (high load, surplus, battery SoC, grid fallback) and controls switch banks.
- `LoadAlertMonitor`: consumes forwarded inverter samples; raises Telegram
  load warnings/recoveries and the `load_high` PagerDuty incident. Its
  load-shed state machine is pure logic in `app/load_alerts.py`.
- `EventProcessor`: fans metrics out to OTEL synchronous gauges and to the
  MQTT, Telegram, and load-alert consumers; notification-only points
  (`switch_event`, `load_alert`) are forwarded but never gauged.
- `TelegramBot` (`app/bot.py`): PULL-binds `URL_WORKER_TELEGRAM`, serves the
  commands, and dispatches bot-initiated notifications.

Rules:

- New concerns (data sources, alert evaluators, bot bridges) get their own
  `AppThread` and register with the nanny.
- Blocking waits use `threads.interruptable_sleep`.
- Plausibility guards (implausible SoC deltas, zero-voltage outputs) must log
  the full supporting data as structured fields before discarding samples.
- State machines and message formatters are pure, dependency-free modules
  (`app/load_alerts.py`, `app/telegram_bot.py`) with fake-clock/unit tests;
  threads only own I/O, scheduling, and lifecycle.

## 3. Serial & Protocol Code (`app/serial_reader.py`)

- `SerialPortReader` runs a background reader thread with an internal frame
  queue; callers pull via `get_result(timeout=...)`.
- The BMS decoder (`app/bms_decoder.py`) is pure logic and fully unit-tested
  (`tests/test_decoder.py`); keep it dependency-free and extend via tests.
- Unknown-but-valid frames get logged with raw hex fields for reverse
  engineering, not dropped silently.

## 4. Alerting & Metrics

- PagerDuty Events API V2: dedup keys are tracked per incident class
  (`bms_heartbeat`, `bms_count`, `load_high`); triggers and resolves must be
  logged with their dedup key as a structured field; seed alert state at
  startup so stale incidents auto-resolve. Failed triggers/resolves retry on
  the next sample.
- Overall-load alerting (`load_warning_w`, `load_critical_w`,
  `load_critical_resolve_seconds`, `load_shed_w`,
  `load_shed_cooldown_seconds`): a Telegram warning is sent once per upward
  crossing, recovery once the load has held below the warning threshold for
  the shed cooldown; PagerDuty trips on a single sample above the critical
  threshold and resolves after the load has held below it for the resolve
  window.
- Switch-bank load shedding: `switch_stats["load_shed"]` is the top-priority
  reason and overrides the inverter-alert restore guard. The latch releases
  only after `load_shed_cooldown_seconds` with no above-threshold sample, and
  every above-threshold sample extends the cooldown; missing load data
  retains the latch.
- OTEL synchronous gauges are named `<point_name>_<metric_key>` with
  attributes from the metrics payload's label set; log-only metrics are
  configured via `[metrics] debug_csv`. Notification-only points
  (`switch_event`, `load_alert`) are forwarded to consumers but never gauged.

## 5. Configuration

- All hardware endpoints/credentials come from `app.conf` sections
  (`app`, `inverter`, `bms`, `weather`, `mqtt`, `telegram`, `metrics`,
  `alert_thresholds`) interpolated from `config/` at container start.
- Thresholds may be optional: read them with
  `app_config.getint(..., fallback=...)` and add the `config/app.conf`
  placeholder (plus the deployment env var) only when a per-deployment
  override is actually provisioned — a placeholder without an env var breaks
  container config interpolation.
- Credentials via pylib `Creds` (1Password); never hardcode API keys.

## 6. Testing & Lint

- `uv run pytest tests/` must pass. Decoder tests (`tests/test_decoder.py`)
  are the safety net for protocol changes; alert state machines
  (`tests/test_load_alerts.py`) and message formatters
  (`tests/test_telegram_bot.py`) are the safety net for behavioural changes.
- Ruff config selects F/E/W/B/I/UP without a custom line length; keep new
  code under 88 columns to avoid adding E501 noise (the file carries
  pre-existing long lines; do not grow that set).
- mypy runs with overrides for hardware/network client libraries.
- Python 3.14: bare tuple `except` clauses (`except A, B:`, PEP 758) are
  valid, and `ruff format` normalizes parenthesized tuple excepts to that
  form — do not "fix" them.
