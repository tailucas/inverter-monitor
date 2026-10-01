---
paths:
  - "app/**"
  - "config/**"
---

# Observability Methodology (inverter-monitor)

This project uses **OpenTelemetry** (OTEL) for metrics, traces, and logs,
exported via OTLP (gRPC or HTTP/protobuf) to any OpenTelemetry Collector or
backend. All OTEL wiring is inherited from `tailucas_pylib` (see
[`tailucas_pylib/__init__.py`](https://github.com/tailucas/pylib)) — the SDK is
configured at import time via environment variables (`OTEL_SDK_DISABLED`,
`OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES`).

## Metrics

- A single application-level meter is created at module scope:
  `OTEL_METER = metrics.get_meter(APP_NAME)`.
- Every metric key reaching `EventProcessor` (from inverter, BMS, weather,
  switch threads) produces an **OTEL synchronous Gauge** named
  `<point_name>_<metric_key>`.
- **Attributes** are derived from the per-point label set — e.g. `bms_addr`,
  `cell`, etc. — and passed as `attributes={...}` to `gauge.set()`.
- Notification-only points (`switch_event`, `load_alert`) are fanned out to
  consumers and never create gauges. The load-shed and overcast latches are
  visible as the `switches_load_shed` and `switches_overcast` gauges via the
  switch stats point. A warn-only (disabled) condition keeps those gauges
  truthful while the switch state stays on (`switches_switch_state` at 1):
  the condition gauge trips with no control publish and one WARNING per
  episode.
- **Timing gauges** are created at module scope using
  `OTEL_METER.create_gauge(...)` and named with `_duration_seconds` or
  `_seconds` suffixes. Each holds the latest measured duration value,
  updated at every sample cycle via `.set(duration)`. Current timing gauges:
  - `inverter_query_duration_seconds` — every fetch attempt (socket round-trip + parse), including failures
  - `inverter_cycle_duration_seconds` — full cycle (query + plausibility + publish)
  - `weather_fetch_duration_seconds` — OpenWeather API HTTP round-trip
  - `bms_frame_process_duration_seconds` — from frame receipt to ZMQ publish
  - `mqtt_publish_duration_seconds` — traceparent injection + client.publish
  - `event_process_duration_seconds` — InfluxDB + OTEL gauge + fan-out per event
  - `gemini_image_duration_seconds` — Gemini text-to-image round trip for `/imagine` (includes a transient-404 retry when one is needed)
- The cadence gauge `inverter_poll_backoff_seconds` is a synchronous Gauge
  holding the current poll backoff applied by the inverter reader (the base
  value after a successful poll, doubled up to 60 s after failures or
  implausible samples). Reaching the 60 s maximum logs a single ERROR per
  outage episode; staying latched at the maximum does not repeat it.
- Log-only metrics (configured via `[metrics] debug_csv`) are discarded after
  debug-logging; they never become OTEL gauges.
- `EventProcessor` has no time-series database writer today: telemetry is
  exported as OTEL gauges and fanned out to the MQTT/Telegram/load-alert
  consumers only.

## Traces

- A module-level tracer (`OTEL_TRACER = trace.get_tracer(APP_NAME)`) is
  available for creating spans around high-value operations.
- **Only MQTT publishes** are wrapped in a
  `tracer.start_as_current_span("mqtt.publish", kind=SpanKind.PRODUCER)` with
  `messaging.*` semantic-convention attributes:
  - `messaging.system = "mqtt"`
  - `messaging.destination.name = <topic>`
  - `messaging.destination_kind = "topic"`
  - `messaging.message.body.size = <payload_bytes>`
  - `traceparent = <generated_traceparent_value>`
- The generated **traceparent** string (`00-{trace_id}-{span_id}-{flags}`) is
  **injected into the MQTT JSON payload** so downstream consumers can continue
  the trace across the messaging boundary.
- A helper `format_traceparent(span)` constructs the string from the span's
  `SpanContext`.
- MQTT *control* publishes are change-gated on the commanded and reported
  states: an unchanged switch state produces no publish, no span and no
  `traceparent`. A bank that keeps reporting a state the command never
  reached is retried at a bounded interval (`switch_retry_seconds`, optional
  `[mqtt]` override) with its span and log at DEBUG, and one WARNING per bank
  and episode names the unacknowledged command.

## Logs

- The `tailucas_pylib` logger (`log`) is bridged into an OTEL `LoggingHandler`
  so all structured logs are also exported via OTLP.
- Log levels follow project-wide conventions (see `logging.md`).

## Level Policy

| Level | Where |
|---|---|
| DEBUG | Per-poll/per-sample/frame tracing, gauge updates (including timing data), "Inverter is delivering power to consumers…" supporting data, inverter fetch failures (resolution, connect/send/receive, empty responses, malformed frames) and poll backoff detail |
| INFO | Startup/lifecycle events, MQTT publishes (`traceparent` in `extra`; switch-bank control publishes also log `topic` and `payload`, and only on an effective commanded/reported change — retries log at DEBUG), switch-bank notifications, load warning/recovery events, PagerDuty triggers & resolves, recoverable failures (PD trigger/resolve failures, retried on the next sample cycle) |
| WARNING | Weather fetch failures (resolution, connect/send/receive, empty responses, malformed payloads), implausible inverter samples, PagerDuty client not configured, missing switch-bank config, readers or Telegram bot disabled at startup, configured switch banks that never reported state (once per bank and episode), switch controllers that did not acknowledge the commanded state (once per bank and episode) |
| ERROR | Lost connections (serial, MQTT), unreadable mappings, inverter poll backoff reaching its maximum (once per outage episode) |
| CRITICAL | Reserved |

## PagerDuty Lifecycle

- Three dedup-key classes: `bms_heartbeat` (data-loss timeout), `bms_count`
  (minimum BMS count violation), and `load_high` (overall load above the
  critical threshold).
- `load_high` trips on a single sample above `load_critical_w` and resolves
  after the load has held at or below it for `load_critical_resolve_seconds`.
  It is seeded stale-pending at startup so a previously-open incident
  auto-resolves, while a high sample at startup still raises the incident.
- Alert state is **seeded at startup** so any previously-open incidents
  auto-resolve on first data.
- Trigger and resolve calls log the `dedup_key` as a structured field.
- Trigger/resolve *failures* log at INFO (recoverable via retry); only
  "client not configured" messages stay at WARNING.

## Shutdown

- `tailucas_pylib.tracing.shutdown()` is called in the `finally` block of
  `main()` (via `die()` → `zmq_term()` currently; ensure OTEL providers are
  flushed before exit).
- ZMQ consumers close their sockets in a `finally` block (`try_close`), and
  the Telegram notification dispatcher task is cancelled by the bot's PTB
  `post_stop` hook.
