---
paths:
  - "app/**"
  - "tests/**"
---

# Structured Logging Standard (inverter-monitor)

All logging in this project is **structured**: a static event message plus an
`extra` dict of `snake_case` fields. Interpolated log messages are prohibited.

## The Logger

```python
from tailucas_pylib import log          # app code
import logging
logger = logging.getLogger(__name__)    # hardware modules (serial_reader.py)
```

Both loggers emit JSON (python-json-logger) configured by `tailucas_pylib`:
stdout below ERROR, stderr from ERROR up, syslog when `SYSLOG_ADDRESS` is set.

## The Pattern

```python
log.debug(
    "Received chunk",
    extra={"data_bytes": len(data), "chunk_number": chunks},
)
log.debug(
    "Socket receive timeout",
    extra={
        "logger_ip": self.logger_ip,
        "logger_port": self.logger_port,
        "logger_sn": self.logger_sn,
        "chunk_number": chunks,
        "error": str(msg),
    },
)
logger.info(
    "Connected to serial port",
    extra={"port": self.port, "baudrate": self.baudrate},
)
```

Never:

```python
log.debug(f"Received {len(data)} bytes for chunk {chunks}.")   # f-string
logger.info("Connected to %s at %d baud", self.port, baudrate)  # %-args
log.info(message.format("RabbitMQ control"))                    # .format()
```

## Rules

1. **Static message names the event; data goes to `extra`.** Keys are
   `snake_case`; values JSON-friendly (coerce with `str()`, `repr()`,
   `.hex()`, `round(...)` where useful).
2. **Exceptions:** `log.exception("Static message", extra={...})` inside
   `except` blocks; `exc_info=True` to attach tracebacks to log records.
   Include `"error": str(e)` when no traceback is attached.
3. **Protocol diagnostics** keep the raw supporting data as fields
   (`header_bytes`, `frame_hex`, `control_code`, `response_bytes`) so failures
   are debuggable from logs alone. Every inverter fetch failure logs at DEBUG
   with the logger context (`logger_ip`, `logger_port`, `logger_sn`) plus
   `chunk_number` and the specific detail (`error`, `response_bytes`, or a
   bounded `response_hex` excerpt). Unclassified fetch errors are logged as
   `"Unexpected error while querying inverter"` with `exc_info=True` and
   return `None` — there are no silent failure returns. Sustained failure
   escalates once: an ERROR (`"Inverter poll backoff reached the maximum"`)
   when the poll backoff first reaches its maximum, re-armed by a successful
   poll.
4. **PagerDuty lifecycle** logs always carry `dedup_key`; trigger/resolve
   failures are INFO (recoverable — retried on the next sample cycle);
   "client not configured" messages stay WARNING.
5. **No secrets** in logs (API keys, tokens, passwords).
6. **Hot loops:** sample chatty per-sample debug logs, and gate expensive
   field construction (e.g. `inverter_supporting_fields`) on
   `log.level == logging.DEBUG`.
7. **MQTT traceparent injection:** Every MQTT publish is wrapped in an OTEL
   span (`mqtt.publish`, `SpanKind.PRODUCER`). The generated traceparent is
   injected into the JSON payload (`"traceparent": "00-..."`) and logged as
   a structured field. Switch-bank control publishes are change-gated on the
   commanded and reported states: an effective change logs at INFO with the
   topic, the published payload and `reported_state`, while a retry of a
   command the bank has not followed logs at DEBUG (`"Retrying switch bank
   control message"`, plus `"Switch bank control retry deferred"` while the
   retry interval has not yet elapsed):

   ```python
   with OTEL_TRACER.start_as_current_span("mqtt.publish", kind=SpanKind.PRODUCER) as span:
       span.set_attribute("messaging.system", "mqtt")
       span.set_attribute("messaging.destination.name", topic)
       tp = format_traceparent(span)
       span.set_attribute("traceparent", tp)
       payload_obj["traceparent"] = tp
       payload = json.dumps(payload_obj)
       client.publish(topic=topic, payload=payload)
       log.info(
           "Switch bank control message published",
           extra={"topic": topic, "payload": payload, "traceparent": tp},
       )
   ```

   The per-sample telemetry publish (`inverter/state`) stays at DEBUG with
   `topic`, `message_bytes` and `traceparent`; its payload is the whole
   inverter sample and it runs at poll cadence.

8. **Tests** must assert on structured fields (`caplog.records` attributes) or
   static message text, never interpolated content.
9. **Bot notifications:** log only structured fields (`kind`, `reason`,
   `switch_banks`, `recipient_count`); never log the rendered user-facing
   message. Message composition belongs to the pure formatters in
   `app/telegram_bot.py`.

## Levels

| Level | Use here |
|---|---|
| DEBUG | per-sample/chunk/frame tracing, gauge updates, "Inverter is delivering power to consumers from backup…" supporting data (always, not conditional), inverter fetch failures (resolution, connect/send/receive, empty responses, malformed frames) and poll backoff detail |
| INFO | reader lifecycle, switch state changes and switch-bank notifications, load warning/recovery events, MQTT publishes (switch-bank control publishes only on an effective commanded/reported change; retries log at DEBUG), PagerDuty triggers & resolves, startup, recoverable warnings (PD trigger/resolve failures) |
| WARNING | weather fetch failures (address resolution, connect/send/receive timeouts and errors, empty responses, malformed payloads), implausible inverter samples, non-recoverable config gaps (PagerDuty client not configured, missing switch-bank config, reader or Telegram bot disabled), configured switch banks that never reported state (once per bank, re-armed by a state message), switch controllers that did not acknowledge the commanded state (once per bank, re-armed by convergence) |
| ERROR | lost connections (serial, MQTT), unreadable mappings, inverter poll backoff reaching its maximum |
| CRITICAL | reserved |
