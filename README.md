<a name="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

## About The Project

### Overview

**Note**: See my write-up on [Inverter Monitoring][blog-url] for architectural context and a sample InfluxDB dashboard setup.

This project extends **base-app** from [GitHub repository][baseapp-url] and hosted on [Docker Hub][baseapp-image-url]. It takes a git submodule dependency on [tailucas-pylib][pylib-url] for shared patterns and utilities.

### Core Functionality

A multi-threaded Python application that interfaces with Deye/Sunsynk hybrid inverters and HinaESS Hi-5 battery management systems for comprehensive solar energy monitoring. It collects telemetry from inverters via local Wi-Fi logger and from BMS units via RS485 serial, correlates this with weather data, and publishes everything to a time-series database with real-time metrics export.

### Key Features

| Feature | Description |
|---|---|
| **Inverter Monitoring** | Direct socket communication with Deye/Sunsynk Wi-Fi logger using proprietary binary protocol with CRC16-MODBUS checksum validation. Reads multi-chunk register data covering solar production, battery state, grid interaction, and load consumption. |
| **Battery Monitoring** | RS485 serial protocol decoder for HinaESS Hi-5 batteries — reads individual cell voltages (16 per BMS unit), pack voltage, temperature sensors, charge/discharge status, and derived current across multiple BMS units. |
| **Weather Correlation** | Fetches current conditions from OpenWeather API and estimates theoretical solar output based on cloud cover and time of day. |
| **Time-Series Storage** | Asynchronous writes to InfluxDB with per-field tagging for device, application, and BMS unit identification. |
| **Prometheus Metrics** | Exposes all inverter, battery, and BMS metrics as Prometheus gauges on port 9401. |
| **MQTT Integration** | Publishes inverter state to MQTT topics and subscribes to control topics for remote switch management. |
| **Smart Switching** | Evaluates battery state-of-charge, load draw, and grid status to make decisions about switching off non-essential consumers via MQTT-controlled switches. |
| **Alerting & Paging** | PagerDuty Events API v2 integration for critical alerts including BMS data loss and minimum BMS unit count violations. |
| **Error Tracking** | Sentry SDK integration with threading and async support for production error monitoring. |
| **Health Monitoring** | Healthchecks.io and Cronitor integration for uptime tracking. |
| **Data Validation** | Plausibility checks with configurable tolerance thresholds, zero-value anomaly detection, and statistical filtering. |
| **Automatic Retries** | Exponential backoff on network and serial port failures with graceful error recovery. |

### Architecture

The application is built around a modular, event-driven architecture using ZeroMQ (in-process messaging) for thread communication:

```
┌─────────────────────────────────────────────────────────────────┐
│                        EventProcessor                           │
│  (central consumer — InfluxDB writes, Prometheus gauges,        │
│   MQTT publishing, debug logging)                               │
└──────▲────────────────────▲─────────────────────▲───────────────┘
       │                    │                     │
       │ ZMQ inproc         │ ZMQ inproc          │ ZMQ inproc
       │                    │                     │
┌──────┴──────┐   ┌────────┴────────┐   ┌────────┴──────────────┐
│ LoggerReader│   │   BmsReader     │   │   WeatherReader       │
│ (TCP socket │   │ (RS485 serial)  │   │ (HTTP — OpenWeather)  │
│  to Deye    │   │  to HinaESS     │   │                      │
│  Wi-Fi      │   │  batteries)     │   │                      │
│  logger)    │   │                 │   │                      │
└─────────────┘   └────────┬────────┘   └──────────────────────┘
                           │ pyserial
                           ▼
                    ┌──────────────┐     ┌──────────────────────┐
                    │SerialPortRead│────▶│  bms_decoder.py      │
                    │er           │     │  (protocol parser)    │
                    └──────────────┘     └──────────────────────┘

┌──────────────────────┐
│   MqttSubscriber     │◀─── MQTT broker ─── Switch devices
│  (control topics)    │
└──────────────────────┘
```

**Threads and their responsibilities:**

- **`LoggerReader`** — Connects to the Deye Wi-Fi logger via TCP (port 8899), constructs binary protocol frames, fetches two chunks of 54 registers each, parses responses with proper endianness and scaling, and publishes structured data to the internal ZMQ socket.
- **`BmsReader`** — Drives the `SerialPortReader` which reads from `/dev/ttyUSB0` (9600 8N1), extracts and decodes BMS frames via the protocol decoder, assigns friendly names (BMS01, BMS02, etc.) per address, tracks per-unit health, and publishes battery metrics with cell-level granularity.
- **`WeatherReader`** — Periodically fetches current weather from OpenWeather API and calculates a theoretical sun production multiplier.
- **`MqttSubscriber`** — Maintains state for MQTT-controlled switch devices, evaluates inverter conditions (battery SOC, power draw, grid mode) to make automated switching decisions, and publishes status updates.
- **`EventProcessor`** — Central consumer that receives all telemetry events, writes to InfluxDB, updates Prometheus gauges, performs debug metrics logging, and handles graceful shutdown.

### Project Structure

```
.
├── app/                        # Application package
│   ├── __init__.py
│   ├── __main__.py             # Main entry point with all threads
│   ├── bms_decoder.py          # HinaESS BMS RS485 protocol decoder
│   └── serial_reader.py        # Serial port reader & frame synchronizer
├── config/
│   ├── app.conf                # Application configuration template
│   └── field_mappings.txt      # Deye inverter register field definitions
├── tests/
│   ├── test_decoder.py         # BMS decoder unit tests
│   ├── bmsdata1.bin            # BMS serial capture (test fixture)
│   └── bmsdata2.bin            # BMS serial capture (test fixture)
├── Dockerfile                  # Production container build
├── docker-compose.yml          # Container orchestration
├── Taskfile.yml                # Build & run tasks (Task runner)
├── Makefile                    # Dev container management
├── pyproject.toml              # Python project metadata & tooling config
├── base.env                    # Base environment configuration
├── dot_env_setup.sh            # Credential-based .env generator
└── .devcontainer/              # VS Code dev container configuration
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With / Integrations

[![Python][python-shield]][python-url]
[![InfluxDB][influxdb-shield]][influxdb-url]
[![Prometheus][prometheus-shield]][prometheus-url]
[![MQTT][mqtt-shield]][mqtt-url]
[![Sentry][sentry-shield]][sentry-url]
[![1Password][1p-shield]][1p-url]

- **Python ≥ 3.12** with uv package manager
- **ZeroMQ** — In-process thread communication
- **InfluxDB** — Time-series metric storage
- **Prometheus** — Metrics endpoint (port 9401)
- **MQTT (Paho)** — Message broker integration
- **PagerDuty** — Critical alerting
- **Sentry** — Error tracking
- **Healthchecks.io / Cronitor** — Uptime monitoring
- **OpenWeather API** — Weather correlation
- **pyserial** — RS485 serial communication
- **libscrc** — CRC16-MODBUS checksums

### Getting Started

#### Prerequisites

- Docker with Compose plugin (for container deployment)
- 1Password Connect server for credential management
- MQTT broker address for switch control
- OpenWeather API key (free tier sufficient)
- RS485-to-USB adapter connected to HinaESS BMS (optional, for battery monitoring)

#### Configuration

1. Clone the repository:
   ```bash
   git clone https://github.com/tailucas/inverter-monitor.git
   cd inverter-monitor
   ```

2. Set up the data directory:
   ```bash
   task datadir
   ```

3. Configure environment variables via 1Password Connect:
   ```bash
   task configure
   ```

4. Run the application:
   ```bash
   task run
   ```

Key configuration items (see `base.env` and `config/app.conf`):
- `INVERTER_LOGGER_ADDRESS` — IP of the Deye Wi-Fi logger on your network
- `INVERTER_LOGGER_SN` — Serial number of the inverter
- `BMS_SERIAL_PORT` — Serial device for BMS (e.g., `/dev/ttyUSB0`)
- `MQTT_SERVER_ADDRESS` — MQTT broker hostname
- `WEATHER_COORD` — Latitude,longitude for weather data
- `INFLUXDB_BUCKET` — Target InfluxDB bucket name

#### Development

For local development without Docker:

```bash
# Set up Python virtual environment
uv python install
uv sync

# Run tests
uv run pytest

# Run linting
uv run ruff check
uv run mypy app/
```

### Sample Metrics

**Inverter Data (per sample):**
```json
{
  "pv1_power_w": 2150,
  "pv2_power_w": 1800,
  "battery_soc_pct": 78,
  "battery_power_w": -450,
  "battery_voltage_v": 48.5,
  "inverter_power_w": 3200,
  "load_power_w": 2800,
  "grid_power_w": 0
}
```

**BMS Data (per BMS unit):**
```json
{
  "addr": 1,
  "voltage_v": 52.8,
  "cell_count": 16,
  "cells_v": [3.285, 3.287, 3.286, ...],
  "min_cell_v": 3.285,
  "max_cell_v": 3.299,
  "cell_diff_mv": 14.0,
  "current_a": -0.43,
  "charging": false,
  "discharging": true,
  "temps": [{"celsius": 21.5}, {"celsius": 22.3}]
}
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>
<!-- LICENSE -->
## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ACKNOWLEDGMENTS -->
## Acknowledgments

* Thank you [jlopez77](https://github.com/jlopez77) for providing the [Deye Inverter Protocol Translation](https://github.com/jlopez77/DeyeInverter) which was shamelessly lifted for this project. For further adaptations, see the ReadMe of that project.
* [Template on which this README is based](https://github.com/othneildrew/Best-README-Template)
* [All the Shields](https://github.com/progfay/shields-with-icon)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/tailucas/inverter-monitor.svg?style=for-the-badge
[contributors-url]: https://github.com/tailucas/inverter-monitor/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/tailucas/inverter-monitor.svg?style=for-the-badge
[forks-url]: https://github.com/tailucas/inverter-monitor/network/members
[stars-shield]: https://img.shields.io/github/stars/tailucas/inverter-monitor.svg?style=for-the-badge
[stars-url]: https://github.com/tailucas/inverter-monitor/stargazers
[issues-shield]: https://img.shields.io/github/issues/tailucas/inverter-monitor.svg?style=for-the-badge
[issues-url]: https://github.com/tailucas/inverter-monitor/issues
[license-shield]: https://img.shields.io/github/license/tailucas/inverter-monitor.svg?style=for-the-badge
[license-url]: https://github.com/tailucas/inverter-monitor/blob/main/LICENSE

[baseapp-url]: https://github.com/tailucas/base-app
[baseapp-image-url]: https://hub.docker.com/repository/docker/tailucas/base-app/general
[pylib-url]: https://github.com/tailucas/pylib

[1p-url]: https://developer.1password.com/docs/connect/
[1p-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=1Password&color=0094F5&logo=1Password&logoColor=FFFFFF&label=
[cronitor-url]: https://cronitor.io/
[healthchecks-url]: https://healthchecks.io/
[influxdb-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=InfluxDB&color=22ADF6&logo=InfluxDB&logoColor=FFFFFF&label=
[influxdb-url]: https://www.influxdata.com/
[mqtt-url]: https://mqtt.org/
[mqtt-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=MQTT&color=660066&logo=MQTT&logoColor=FFFFFF&label=
[ow-url]: https://openweathermap.org/
[prometheus-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Prometheus&color=E6522C&logo=Prometheus&logoColor=FFFFFF&label=
[prometheus-url]: https://prometheus.io/
[python-url]: https://www.python.org/
[python-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Python&color=3776AB&logo=Python&logoColor=FFFFFF&label=
[sentry-url]: https://sentry.io/
[sentry-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Sentry&color=362D59&logo=Sentry&logoColor=FFFFFF&label=
[blog-url]: https://tailucas.github.io/update/2023/06/04/inverter-monitoring.html
