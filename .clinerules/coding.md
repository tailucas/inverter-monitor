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
switch banks via MQTT and Sonoff devices over the LAN.

## 1. Posture

- Hardware-facing code must be defensive: timeouts, retries, plausibility
  checks, and PagerDuty alerting for data loss are first-class concerns.
- No silent failures: every inverter fetch failure returns `None` and logs
  the detail at DEBUG with the logger context (`logger_ip`, `logger_port`,
  `logger_sn`); sustained failure escalates to a single ERROR when the poll
  backoff reaches its 60 s maximum (one per outage episode, re-armed by a
  successful poll).
- Built on the `tailucas_pylib` framework (`AppThread`, `exception_handler`,
  `thread_nanny`, `die()`/`bye()` shutdown). Follow pylib's standards.

## 2. Application Architecture (`app/__main__.py`)

One `AppThread` per concern, wired over ZMQ inproc (`URL_WORKER_APP`,
`URL_WORKER_MQTT_PUBLISH`, `URL_WORKER_LOAD_MONITOR`, `URL_WORKER_TELEGRAM`):

- `LoggerReader`: polls the inverter Wi-Fi logger (chunked binary protocol,
  CRC16-MODBUS validation via `libscrc`, field mappings from
  `config/field_mappings.txt`). Polls continuously with a 600 ms base backoff
  (`poll_backoff_seconds`), doubling exponentially to 60 s after failed or
  implausible samples. On-demand queries from the Telegram bot go through
  `query_now()` (`app/single_flight.py`) so ad-hoc and scheduled polls never
  hit the logger socket concurrently. Fetch errors never propagate:
  `get_logger_data()` logs unexpected exceptions at DEBUG with traceback and
  returns `None`, which the poll loop turns into a backoff (a run of failures
  that reaches the 60 s maximum logs one ERROR). Request frames
  byte-swap the logger serial from an 8-hex-digit zero-padded value
  (`f"{self.logger_sn:08x}"`) so short or leading-zero serials stay valid.
- `BmsReader`: consumes decoded BMS frames from `SerialPortReader`
  (`app/serial_reader.py`); assigns friendly BMS names, derives scalars,
  manages PagerDuty heartbeat/count incidents.
- `WeatherReader`: OpenWeather sampling correlated with inverter data; polls
  at `[weather] poll_interval_seconds` (default 60 s), HTTP requests time out
  after 10 s, and failed or malformed fetches back off exponentially (up to
  600 s) and reset on success.
- `MqttSubscriber`: consumes EventProcessor-forwarded inverter samples and
  caches forwarded weather samples (overcast rationing); subscribes to
  `{topic_prefix}/state/#`, applies the rationing checks (high load,
  overcast, surplus, battery SoC, grid fallback) and controls switch banks.
  Every control publish to `{topic_prefix}/control/{bank}` logs at INFO
  (`"Switch bank control message published"`) with its `topic` and
  `payload`. A bank is only controllable once it has reported its state to
  the subscription topic (the change gate needs its current switch states);
  a configured bank that never reports is left alone and logs one WARNING
  per bank and episode (`"Switch banks without reported state were not
  controlled"`, re-armed by a state message, with `configured_banks`,
  `unreported_banks`, `subscription_topic` and `state_age_secs`). The
  manual-command record (`"Manual switch command applied"`) carries the bank
  visibility (`configured_banks`, `known_banks`, `unchanged_banks`,
  `state_age_secs`). Every decision is also handed to the `SonoffController`
  thread (`app/sonoff.py`), which issues the LAN-mode control messages for
  the configured Sonoff devices.
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
- Sonoff LAN-mode control (`app/sonoff.py`) builds the documented
  AES-128-CBC payload (MD5 of the *device key*, random IV, compact JSON
  switch command) and POSTs it to `http://{address}:8081/zeroconf/switch`
  with a 3 s timeout — devices are addressed by their 1Password address, so
  no mDNS discovery, cloud round-trip or event loop is involved. The
  credential is `Sonoff/{id}/devicekey` (the eWeLink device key) — the account
  `apikey` is a different credential, is never read, and would be rejected by
  the device with `error 400`. The controller verifies each key at start-up
  with a read-only `/zeroconf/info` probe: a device that answers
  `error 400/401` logs an actionable WARNING. Every issued control message
  logs at INFO (`"Sonoff control message issued"`) with the device's HTTP
  `response_status` and bounded `response_body`; failed control messages
  log a WARNING with the device's bounded `response_body` and the same
  `error_hint` before backing off per device (5 s doubling to a 60 s cap,
  one ERROR when the cap is reached, re-armed by a success).

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
- Sonoff devices follow the same decision as the MQTT banks: every device is
  switched off on a shed, and a restore only switches on devices whose
  `Sonoff/{id}/shed_only` credential is false (default true: only ever
  switch off). Commands are change-gated on the last *successfully commanded*
  state, so a failed command is retried by later decisions and the controller
  thread never blocks the inverter decision loop.
- Manual override: `/startloadshed` and `/endloadshed` queue a command on the
  `MqttSubscriber` (`request_switch_command`, drained every loop iteration)
  that force or reset the cooldown latches through the pure
  `manual_switch_decision` (`app/load_alerts.py`) and publish the same
  switch-bank decision with the `manual_load_shed` / `manual_restore` reasons,
  so the Sonoff `shed_only` semantics are preserved and a still-live trip
  condition re-sheds on the next sample.
- Overcast rationing: `switch_stats["overcast"]` trips while the latest
  weather sample reports 100 % cloudiness, with the same self-extending
  cooldown (`overcast_cooldown_seconds`, default 3600 s); the inverter-alert
  restore guard still wins over it. Weather older than ~180 s is treated as
  unknown and retains the latch.
- The surplus rationing check averages
  `pv1_power_w + pv2_power_w - battery_power_w` over
  `generation_average_seconds` (default 300 s) so the decision smoothing is
  independent of the inverter poll rate.
- OTEL synchronous gauges are named `<point_name>_<metric_key>` with
  attributes from the metrics payload's label set; log-only metrics are
  configured via `[metrics] debug_csv`. Notification-only points
  (`switch_event`, `load_alert`) are forwarded to consumers but never gauged.

## 5. Configuration

- All hardware endpoints/credentials come from `app.conf` sections
  (`app`, `inverter`, `bms`, `weather`, `mqtt`, `sonoff`, `telegram`,
  `metrics`, `alert_thresholds`) interpolated from `config/` at container
  start.
- Thresholds may be optional: read them with
  `app_config.getint(..., fallback=...)` and add the `config/app.conf`
  placeholder (plus the deployment env var) only when a per-deployment
  override is actually provisioned — a placeholder without an env var breaks
  container config interpolation.
- Credentials via pylib `Creds` (1Password); never hardcode API keys.

## 6. Testing & Lint

- `uv run pytest tests/` must pass. Decoder tests (`tests/test_decoder.py`)
  and the Sonoff LAN-mode payload vector (`tests/test_sonoff.py`) are the
  safety net for protocol changes; alert state machines
  (`tests/test_load_alerts.py`), Sonoff decision logic and controller
  behaviour (`tests/test_sonoff.py`), message formatters
  (`tests/test_telegram_bot.py`), and Telegram data helpers
  (`tests/test_bot_helpers.py`) are the safety net for behavioural changes.
- Ruff config selects F/E/W/B/I/UP without a custom line length; keep new
  code under 88 columns to avoid adding E501 noise (the file carries
  pre-existing long lines; do not grow that set).
- mypy runs with overrides for hardware/network client libraries.
- Python 3.14: bare tuple `except` clauses (`except A, B:`, PEP 758) are
  valid, and `ruff format` normalizes parenthesized tuple excepts to that
  form — do not "fix" them.
