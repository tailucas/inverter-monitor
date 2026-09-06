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
log.warning(
    "Socket receive timeout",
    extra={"error": str(msg)},
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
   `except` blocks; `exc_info=True` to attach tracebacks to warnings. Include
   `"error": str(e)` when no traceback is attached.
3. **Protocol diagnostics** keep the raw supporting data as fields
   (`header_bytes`, `frame_hex`, `control_code`, `response_bytes`) so failures
   are debuggable from logs alone.
4. **PagerDuty lifecycle** logs always carry `dedup_key`; trigger/resolve
   failures are INFO (recoverable — retried on next cycle); "client not
   configured" messages stay WARNING.
5. **No secrets** in logs (API keys, tokens, passwords).
6. **Hot loops:** sample chatty debug logs (see the `randint(0, 1000)`
   guards in the ADC sampling loop) and gate expensive field construction on
   `log.level == logging.DEBUG`.
7. **MQTT traceparent injection:** Every MQTT publish is wrapped in an OTEL
   span (`mqtt.publish`, `SpanKind.PRODUCER`). The generated traceparent is
   injected into the JSON payload (`"traceparent": "00-..."`) and logged as
   a structured field. Log at INFO:

   ```python
   with OTEL_TRACER.start_as_current_span("mqtt.publish", kind=SpanKind.PRODUCER) as span:
       span.set_attribute("messaging.system", "mqtt")
       span.set_attribute("messaging.destination.name", topic)
       tp = format_traceparent(span)
       span.set_attribute("traceparent", tp)
       payload_obj["traceparent"] = tp
       payload = json.dumps(payload_obj)
       client.publish(topic=topic, payload=payload)
       log.debug("MQTT message dispatched", extra={"topic": topic, "traceparent": tp})
   ```

8. **Tests** must assert on structured fields (`caplog.records` attributes) or
   static message text, never interpolated content.

## Levels

| Level | Use here |
|---|---|
| DEBUG | per-sample/chunk/frame tracing, gauge updates, "Inverter is delivering power to consumers from backup…" supporting data (always, not conditional) |
| INFO | reader lifecycle, switch state changes, MQTT publishes, PagerDuty triggers & resolves, startup, recoverable warnings (timeouts, implausible samples, PD trigger/resolve failures, InfluxDB write failures) |
| WARNING | non-recoverable config gaps (PagerDuty client not configured, missing switch-bank config, reader disabled) |
| ERROR | lost connections (serial, MQTT), unreadable mappings |
| CRITICAL | reserved |
