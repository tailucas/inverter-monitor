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
7. **MQTT traceparent injection:** Every MQTT publish goes through
   `mqtt_publish_span(topic, traced=...)` (`app/__main__.py`), which yields
   the OTEL span carrying the `messaging.*` semantic conventions. A
   legitimate event (the change-gated switch-bank control publish) gets a
   real `mqtt.publish`/`SpanKind.PRODUCER` span. The cadence
   `inverter/state` telemetry publish is a **heartbeat**, so it yields a
   non-recording span by default: the payload still gets a traceparent, but
   no span is exported per sample. The generated traceparent is injected into
   the JSON payload (`"traceparent": "00-..."`) and logged as a structured
   field; a heartbeat traceparent is flagged **unsampled** (`-00`) so a
   parent-based sampler downstream drops it, while an event span propagates
   `-01`. Switch-bank control publishes are change-gated on the
   commanded and reported states: an effective change logs at INFO with the
   topic, the published payload and `reported_state`, while a retry of a
   command the bank has not followed logs at DEBUG (`"Retrying switch bank
   control message"`, plus `"Switch bank control retry deferred"` while the
   retry interval has not yet elapsed):

   ```python
   with mqtt_publish_span(topic) as span:
       span.set_attribute("messaging.message.body.size", len(payload))
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
   inverter sample and it runs at poll cadence, so it calls
   `mqtt_publish_span("inverter/state", traced=False)`. The optional
   `[mqtt] trace_telemetry_publishes` (code default false) opts back into an
   exported span per sample; the resolved policy logs once at startup
   (`"Telemetry publish trace policy resolved"`).

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
