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
   failures are WARNING with `exc_info=True`.
5. **No secrets** in logs (API keys, tokens, passwords).
6. **Hot loops:** sample chatty debug logs (see the `randint(0, 1000)`
   guards in the ADC sampling loop) and gate expensive field construction on
   `log.level == logging.DEBUG`.
7. **Conditional level, same event:** keep message + fields in variables:

   ```python
   log_msg = "Inverter is delivering power to consumers from backup (solar/battery)"
   log_fields = {"inverter_power_w": inverter_power_w, "switch_state": switch_state, ...}
   if prev_switch_state != switch_state:
       log.info(log_msg, extra=log_fields)
   elif log.level == logging.DEBUG:
       log.debug(log_msg, extra=log_fields)
   ```

8. **Tests** must assert on structured fields (`caplog.records` attributes) or
   static message text, never interpolated content.

## Levels

| Level | Use here |
|---|---|
| DEBUG | per-sample/chunk/frame tracing, gauge updates |
| INFO | reader lifecycle, switch state changes, PD resolves, startup |
| WARNING | timeouts, implausible samples, PD trigger failures, degraded config |
| ERROR | lost connections (serial, MQTT), unreadable mappings |
| CRITICAL | reserved |
