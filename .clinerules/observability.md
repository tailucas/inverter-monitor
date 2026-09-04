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
- Log-only metrics (configured via `[metrics] debug_csv`) are discarded after
  debug-logging; they never become OTEL gauges.
- InfluxDB writes remain feature-flagged (`local-influxdb`) and are written
  alongside the gauge update.

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

## Logs

- The `tailucas_pylib` logger (`log`) is bridged into an OTEL `LoggingHandler`
  so all structured logs are also exported via OTLP.
- Log levels follow project-wide conventions (see `logging.md`).

## Level Policy

| Level | Where |
|---|---|
| DEBUG | Per-poll/per-sample/frame tracing, gauge updates, "Inverter is delivering power to consumers…" supporting data |
| INFO | Startup/lifecycle events, MQTT publishes (with `traceparent` in `extra`), PagerDuty triggers & resolves, recoverable warnings (socket timeouts, implausible samples, PD trigger/resolve failures, InfluxDB write failures) |
| WARNING | PagerDuty client not configured, missing switch-bank config, reader disabled at startup |
| ERROR | Lost connections (serial, MQTT), unreadable mappings |
| CRITICAL | Reserved |

## PagerDuty Lifecycle

- Two dedup-key classes: `bms_heartbeat` (data-loss timeout) and `bms_count`
  (minimum BMS count violation).
- Alert state is **seeded as triggered at startup** so any previously-open
  incidents auto-resolve on first data.
- Trigger and resolve calls log the `dedup_key` as a structured field.
- Trigger/resolve *failures* log at INFO (recoverable via retry); only
  "client not configured" messages stay at WARNING.

## Shutdown

- `tailucas_pylib.tracing.shutdown()` is called in the `finally` block of
  `main()` (via `die()` → `zmq_term()` currently; ensure OTEL providers are
  flushed before exit).