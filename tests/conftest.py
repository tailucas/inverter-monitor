"""Test-session bootstrap.

Disables the OpenTelemetry SDK before any test module imports the
application. `tailucas_pylib` wires real OTLP batch exporters (gRPC,
`localhost:4317` by default) at import time; with no collector running
during tests, every flush logs "Failed to export ... Connection refused"
to stderr at interpreter shutdown. `OTEL_SDK_DISABLED` is read by the SDK
when the providers are constructed, so it must be set here, before pytest
imports the test modules. An explicitly exported value still wins.
"""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
