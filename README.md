<a name="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

## About The Project

### Overview

**Note 1**: See my write-up on [Inverter Monitoring][blog-url] for architectural context and a sample InfluxDB dashboard setup.

**Note 2**: This project **extends base-app** from [GitHub repository][baseapp-url] and hosted on [Docker Hub][baseapp-image-url]. It takes a git submodule dependency on [tailucas-pylib][pylib-url] for shared patterns and utilities.

### Core Functionality

This is a **715-line Python application** that interfaces with Deye/Sunsynk inverters via local Wi-Fi logger to:
- **Fetch inverter telemetry**: Real-time power generation, battery state, voltage, current across multiple data chunks
- **Validate data**: Detects implausible readings and applies statistical filtering (±5% tolerance)
- **Publish to InfluxDB**: Time-series database integration with Prometheus metrics export
- **Publish to MQTT**: Topic-based messaging for automation and monitoring
- **Weather correlation**: Fetches weather data from OpenWeather API for solar production analysis
- **Smart switching**: Evaluates battery state and load to make decisions about switching off consumers

The application is optimized for **continuous monitoring** of off-grid or hybrid solar setups with battery storage, providing both real-time metrics and historical analysis.

**Key Features:**

* **Deye Inverter Protocol**: Direct socket communication with Deye/Sunsynk Wi-Fi logger using proprietary binary protocol with CRC16-MODBUS checksum validation
* **Multi-Chunk Data Fetching**: Reads two 54-register chunks (registers 0x003B-0x0070, 0x0096-0x00C2) with proper byte swapping and two's complement handling
* **Plausibility Checking**: Filters implausible battery SOC changes (>5% within 120 seconds) and zero-value anomalies
* **InfluxDB Integration**: Asynchronous writes to InfluxDB with per-field tags for device and application
* **Prometheus Metrics**: Exposes all inverter metrics as Prometheus gauges on port 8000
* **Weather-Based Heuristics**: Correlates cloud cover and sun position to production estimates
* **RabbitMQ-style Messaging**: MQTT topic-based messaging for switch control and status updates
* **Error Recovery**: Automatic retries with exponential backoff on network failures
* **Sentry Integration**: Production error tracking with threading and async support
* **Health Monitoring**: Healthchecks.io and Cronitor integration for uptime tracking

### Architecture & Design

This 715-line Python application (`app/__main__.py`) demonstrates patterns for building production-grade IoT telemetry systems with multi-threaded data collection:

**Core Components** (line numbers):

* **`LoggerReader`** (line 72): Thread that connects to Deye Wi-Fi logger via TCP socket on port 8899, constructs binary protocol frames with CRC checksums, reads 2 chunks of 54 registers each, parses response data with endianness conversion, applies scaling factors and units (kW, V, A, °C), and publishes to internal ZMQ socket
* **`WeatherReader`** (line 320): Thread that fetches current weather from OpenWeather API using latitude/longitude coordinates, calculates theoretical sun output based on sunrise/sunset times and cloud cover percentage, and publishes to ZMQ socket on 5-minute intervals
* **`MqttSubscriber`** (line 388): Thread that subscribes to MQTT control topics, tracks state changes for switch devices (CSV-configured), publishes inverter data to MQTT topic prefix, and handles connection recovery
* **`EventProcessor`** (line 577): Central data aggregation thread that receives messages from all readers via ZMQ socket, writes to InfluxDB asynchronously, creates dynamic Prometheus gauges, and relays inverter data to MQTT publisher
* **`field_mappings.txt`**: JSON file with 100+ register definitions mapping Deye protocol register addresses (0x00BA, 0x00BB, etc.) to human-readable field names with scaling ratios and units

**Data Flow:**

1. LoggerReader connects to inverter logger, reads register chunks with CRC validation
2. Parses binary response, applies scaling ratios (e.g., voltage * 0.1)
3. Validates plausibility: checks for zero anomalies and ±5% SOC deltas
4. Sends validated data to EventProcessor via `inproc://app` ZMQ socket
5. WeatherReader independently fetches weather every 60 seconds
6. EventProcessor receives both streams, writes points to InfluxDB
7. Prometheus gauges updated in real-time for metrics scraping
8. MqttSubscriber listens for switch control commands
9. Inverter data republished to MQTT `{topic_prefix}/...` for other systems

**Configuration Pattern:**

- INI-format `config/app.conf` with variable interpolation from 1Password Fleet variables via `%(VARIABLE_NAME)s` syntax
- 12 configuration variables covering inverter logger, InfluxDB, MQTT, weather, and health monitoring
- Field mappings externalized to `config/field_mappings.txt` for decoupling from code

**Technology Patterns:**

- **Deye Protocol**: Binary socket communication with CRC16-MODBUS checksums for data integrity
- **ZeroMQ (inproc)**: Thread-safe inter-component messaging with PUSH/PULL/PULL sockets
- **InfluxDB**: Asynchronous write API for time-series data with per-field tags
- **Prometheus**: Gauge metrics for Grafana integration and alerting
- **MQTT**: Pub/sub for integrating with Home Assistant, Node-Red, other automation
- **OpenWeather API**: REST API for weather correlation and sun position calculation
- **Sentry SDK**: Production error tracking with threading and async support
- **tailucas-pylib**: Shared patterns (CredsConfig, SignalHandler, AppThread, exception_handler, thread_nanny)

See [tailucas-pylib][pylib-url] for shared patterns and [base-app][baseapp-url] for the container foundation.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

Technologies that help make this package useful:

[![1Password][1p-shield]][1p-url]
[![InfluxDB][influxdb-shield]][influxdb-url]
[![Python][python-shield]][python-url]
[![MQTT][mqtt-shield]][mqtt-url]
[![Sentry][sentry-shield]][sentry-url]
[![ZeroMQ][zmq-shield]][zmq-url]

Also:

* [Cronitor][cronitor-url]
* [Healthchecks][healthchecks-url]
* [OpenWeather][ow-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->
## Getting Started

Here is some detail about the intended use of this package.

### Prerequisites

Beyond the Python dependencies defined in [pyproject.toml](pyproject.toml), the project requires:

* **[1Password Secrets Automation][1p-url]**: Runtime credential and configuration management (paid with free tier)
* **[Sentry][sentry-url]**: Error tracking and monitoring (free tier available)
* **[InfluxDB Cloud][influxdb-url]**: Time-series database for telemetry (free tier available with limits)
* **[MQTT Broker][mqtt-url]**: Message broker for device communication (self-hosted or managed service like HiveMQ)
* **[OpenWeather API][ow-url]**: Weather data integration (free tier available)
* **Deye/Sunsynk Inverter**: With Wi-Fi logger connected to local network

Optional services:
* **[Healthchecks.io][healthchecks-url]**: Health check monitoring (free tier available)
* **[Cronitor][cronitor-url]**: Cron job and process monitoring
* **[Prometheus/Grafana][prometheus-url]**: Metrics collection and visualization

### Required Tools

Install these tools before setting up the project:

* **`task`**: Build orchestration - https://taskfile.dev/installation/#install-script
* **`docker`** and **`docker-compose`**: Container runtime - https://docs.docker.com/engine/install/
* **`uv`**: Python package manager - https://docs.astral.sh/uv/getting-started/installation/

For local development (optional):
* **`python3`**: Python 3.12+ runtime

### Installation

:stop_sign: **1Password Secrets Automation Required**: This project stores all configuration and credentials via [1Password Secrets Automation][1p-url]. A 1Password Connect server must be running in your environment. If you prefer not to use this, fork the project and modify the credential loading logic in `app/__main__.py` (lines 46-48, 323, 608, 615-616).

#### Step 1: Configure 1Password Secrets

Your 1Password Secrets Automation vault must contain an entry called `ENV.inverter_monitor` with the following configuration variables:

| Variable | Purpose | Example |
|---|---|---|
| `APP_NAME` | Application identifier for logging | `inverter_monitor` |
| `DEVICE_NAME` | Container hostname | `inverter-monitor` |
| `CRONITOR_MONITOR_KEY` | Cronitor health check API key | *specific to your account* |
| `HC_PING_URL` | Healthchecks.io ping URL for heartbeat | *specific to your check* |
| `INFLUXDB_BUCKET` | InfluxDB bucket name for telemetry | `inverter` |
| `INVERTER_LOGGER_ADDRESS` | IP address of Deye Wi-Fi logger | `192.168.1.100` |
| `INVERTER_LOGGER_PORT` | TCP port of logger (default Deye) | `8899` |
| `INVERTER_LOGGER_SN` | Serial number of logger device | *from device label* |
| `INVERTER_LOGGER_SAMPLE_INTERVAL_SECS` | Polling interval (60s minimum) | `60` |
| `MQTT_SERVER_ADDRESS` | IP address of MQTT broker | `192.168.1.200` |
| `MQTT_TOPIC_PREFIX` | Topic prefix for MQTT messages | `switch` |
| `MQTT_SWITCH_DEVICE_CSV` | CSV of switch device IDs for control | `switch1,switch2` |
| `OP_CONNECT_HOST` | 1Password Connect server URL | `http://1password-connect:8080` |
| `OP_CONNECT_TOKEN` | 1Password Connect API token | *specific to your server* |
| `OP_VAULT` | 1Password vault ID | *specific to your vault* |
| `WEATHER_COORD` | Latitude,longitude for OpenWeather API | `-34.05,18.85` |

**Additional Credentials** (stored separately in 1Password):
- `Cronitor/password`: Cronitor API key
- `InfluxDB/local/token`: InfluxDB API token
- `InfluxDB/local/org`: InfluxDB organization name
- `OpenWeather/password`: OpenWeather API key
- `Sentry/{APP_NAME}/dsn`: Sentry DSN for error tracking

#### Step 2: Clone Repository and Submodules

```bash
git clone https://github.com/tailucas/inverter-monitor.git
cd inverter-monitor
git submodule init
git submodule update
```

#### Step 3: Build and Deploy

Using Task CLI (recommended):
```bash
task build      # Build Docker image with dependencies
task configure  # Generate .env from 1Password secrets
task run        # Run container in foreground
task rund       # Run container detached
```

Or using docker-compose directly:
```bash
docker compose --env-file base.env build
docker compose run app /opt/app/dot_env_setup.sh >> .env
docker compose up
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Build System

### Task CLI (Taskfile.yml)

Primary build orchestration:

- `task python` - Setup Python virtual environment with uv
- `task build` - Build Docker image with all dependencies and application code
- `task run` - Run container in foreground with full log output
- `task rund` - Run container detached (persists after terminal close)
- `task configure` - Generate .env configuration from 1Password secrets
- `task datadir` - Create data directory with proper permissions (UID/GID 999)
- `task push` - Push built image to Docker Hub registry

### Dockerfile

Extends `tailucas/base-app:latest` with:
- Additional system packages: html-xml-utils, sqlite3, wget
- Locale generation (en_ZA.UTF-8 by default, configurable via build args)
- Override of base-app cron jobs (removes base_job)
- Custom application entrypoint
- Python dependencies via uv (pyproject.toml)
- Field mappings file for inverter protocol definitions

### Dependencies

**Python** (`pyproject.toml`, managed via uv, requires Python 3.12+):
- `influxdb-client>=1.49.0` - InfluxDB time-series database client
- `libscrc>=1.8.1` - CRC16-MODBUS checksum calculation for Deye protocol
- `paho-mqtt>=2.1.0` - MQTT broker client for messaging
- `prometheus-client>=0.23.1` - Prometheus metrics export
- `requests>=2.32.5` - HTTP client for OpenWeather API
- `sentry-sdk>=2.38.0` - Error tracking and monitoring
- `tailucas-pylib>=0.5.6` - Shared utilities (threading, ZMQ, 1Password, Sentry)

### Configuration

**config/app.conf** (INI format with variable interpolation):
- `[app]`: Device name, Cronitor monitor key
- `[creds]`: Sentry DSN and Cronitor paths
- `[influxdb]`: Bucket name
- `[mqtt]`: Server address, topic prefix, switch devices
- `[inverter]`: Logger address, port, serial number, sample interval
- `[weather]`: Latitude/longitude coordinates

**config/field_mappings.txt** (JSON):
- 100+ Deye protocol register definitions
- Maps register addresses to field names, scaling factors, units
- Organized by category: solar, battery, grid, temperature, etc.

**Docker Compose**:
- Syslog logging to Docker host
- 1Password Connect secret integration
- Volume mounts: `/data` for persistence, `/dev/log` for syslog
- Hostname and environment variable configuration

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Application Components

### Inverter Data Collection

The `LoggerReader` class (line 72) handles communication with Deye Wi-Fi logger:

1. **Socket Connection**: Connects to logger IP:port (default 192.168.1.x:8899)
2. **Binary Protocol**: Constructs Deye protocol frames with:
   - Start byte (0xA5)
   - Frame length and control code
   - Serial number (0x0000)
   - Logger serial number (4 bytes, byte-swapped)
   - Data field (command 0x0203000000...)
   - CRC16-MODBUS checksum (2 bytes)
   - Checksum (1 byte, sum of all frame bytes)
   - End byte (0x15)
3. **Two Chunks**: Reads registers 0x003B-0x0070 then 0x0096-0x00C2
4. **Response Parsing**: Extracts 4-byte register values starting at offset 56
5. **Data Conversion**: Applies scaling ratios (e.g., voltage * 0.1, temperature * ratio - 100)
6. **Field Mapping**: Uses field_mappings.txt to translate register addresses to field names
7. **Sanitization**: Converts field names to lowercase with underscores, removes special chars

### Data Validation

Implements plausibility checking to detect sensor anomalies:

**Battery SOC Validation**:
- Checks for zero anomalies: if SOC=0% and voltage=0V, discards as implausible
- Validates SOC delta: if change > 5% within 120 seconds, discards
- Tracks history to prevent rapid flip-flopping

**Retry Logic**:
- Retries up to 5 times within the sample interval
- 5-second wait between retries
- Logs warnings on repeated failures

### Weather Integration

The `WeatherReader` class (line 320) enriches data with weather context:

1. **API Call**: Fetches current weather from OpenWeather (lat/lon coordinates)
2. **Cloudiness**: Extracts cloud cover percentage
3. **Sun Position**: Calculates theoretical sun output based on:
   - Sunrise/sunset times
   - Current time relative to midday
   - Normalized to 0-100% output potential
4. **Publication**: Sends weather data every 60 seconds to EventProcessor

### InfluxDB Integration

The `EventProcessor` class (line 577) writes telemetry to InfluxDB:

1. **Async Writes**: Uses InfluxDB asynchronous write API (batching for performance)
2. **Point Naming**: Each metric becomes a point (e.g., "inverter", "weather")
3. **Tagging**: Adds tags: `application={APP_NAME}`, `device={DEVICE_NAME_BASE}`
4. **Fields**: Each metric value becomes a field (e.g., pv1_power_w, battery_soc_pct)
5. **Feature Flag**: Writes only if "local-influxdb" feature flag enabled

### MQTT Messaging

The `MqttSubscriber` class (line 388) handles message broker integration:

1. **Subscription**: Subscribes to control topics for configured switch devices
2. **State Tracking**: Maintains dictionary of switch states
3. **Publishing**: Sends inverter data to MQTT topic `{prefix}/...`
4. **Connection Handling**: Auto-reconnect on broker disconnect
5. **Clean Sessions**: Implements MQTT v3.1.1 protocol

### Prometheus Metrics

Real-time metrics export on port 8000:

1. **Gauge Creation**: Dynamic gauges created for each metric (first seen)
2. **Gauge Naming**: Combines point name and field name (e.g., "inverter_pv1_power_w")
3. **Scraping**: Compatible with Prometheus and Grafana

### Health Monitoring

**Sentry Integration**:
- AsyncioIntegration for async support
- ThreadingIntegration for multi-threaded safety
- SysExitIntegration for tracking unexpected exits
- DSN loaded from 1Password Sentry/{APP_NAME}/dsn

**Thread Monitoring**:
- Nanny thread tracks all worker threads
- Cronitor API for thread health reporting
- Healthchecks.io for application heartbeat (5-minute interval)

## Deployment Patterns

### Configuration Flow

1. **1Password Fetch**: CredsConfig loads all `ENV.inverter_monitor` variables
2. **INI Interpolation**: config_interpol substitutes `%(VARIABLE_NAME)s` with actual values
3. **Field Mappings**: JSON file parsed to extract register definitions
4. **Thread Startup**: All components initialized and started in specific order
5. **Metrics Export**: Prometheus server starts on port 8000
6. **Main Loop**: Application waits for signals or worker thread failures

### Error Handling

Application demonstrates robust error recovery:
- **Socket Timeouts**: 10-second timeout per logger connection attempt
- **Network Failures**: Automatic retries with exponential backoff
- **JSON Parse Errors**: Graceful handling of malformed API responses
- **MQTT Disconnects**: Automatic reconnection with clean session flag
- **ZMQ Context**: Properly terminated on shutdown via zmq_term()

### Monitoring & Observability

**Logging**:
- INFO level by default (DEBUG available for troubleshooting)
- Syslog integration for centralized log aggregation
- Structured logging with context (field counts, retry counts, etc.)

**Metrics**:
- Prometheus gauges for real-time metric values
- Prometheus endpoint on port 8000 for scraping
- Grafana-compatible time-series format

**Health Checks**:
- Cronitor thread monitoring (tracks thread count, reports periodically)
- Healthchecks.io heartbeat (checks liveness every 5 minutes)
- Sentry error tracking (captures exceptions and anomalies)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

Running the application will:

1. **Initialize Components**: Loads field mappings, sets up Sentry instrumentation, initializes all worker threads
2. **Connect to Inverter Logger**: Opens TCP socket to Deye logger, begins polling at configured interval
3. **Fetch Weather Data**: Queries OpenWeather API for cloud cover and sun position
4. **Publish to InfluxDB**: Writes all metrics to time-series database asynchronously
5. **Export Prometheus Metrics**: Exposes current values on port 8000 for Grafana/Prometheus scraping
6. **Handle MQTT Messages**: Subscribes to control topics and republishes inverter status
7. **Monitor Health**: Sends periodic heartbeats to Healthchecks.io and Cronitor
8. **Make Smart Decisions**: Evaluates battery SOC and load to determine if switching is needed

### Example InfluxDB Query

Query battery state of charge over time:
```
from(bucket:"inverter")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "inverter" and r._field == "battery_soc_pct")
  |> aggregateWindow(every: 1h, fn: mean)
```

### Example Prometheus Alert

Alert when battery SOC is critical:
```yaml
- alert: BatteryCritical
  expr: inverter_battery_soc_pct < 40
  for: 5m
  annotations:
    summary: "Battery SOC critical ({{ $value }}%)"
```

### Example MQTT Message

Published to `{topic_prefix}/inverter` after each sample:
```json
{
  "pv1_power_w": 2450,
  "pv2_power_w": 1890,
  "total_pv_power_w": 4340,
  "battery_soc_pct": 65,
  "battery_voltage_v": 48.5,
  "inverter_power_w": 3200,
  "load_power_w": 2800,
  "grid_power_w": 0
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
[switch-app-url]: https://github.com/tailucas/switch-app

[1p-url]: https://developer.1password.com/docs/connect/
[1p-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=1Password&color=0094F5&logo=1Password&logoColor=FFFFFF&label=
[cronitor-url]: https://cronitor.io/
[deye-url]: https://www.deyeinverter.com/
[healthchecks-url]: https://healthchecks.io/
[influxdb-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=InfluxDB&color=22ADF6&logo=InfluxDB&logoColor=FFFFFF&label=
[influxdb-url]: https://www.influxdata.com/
[mongoose-os-url]: https://mongoose-os.com/
[mqtt-url]: https://mqtt.org/
[mqtt-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=MQTT&color=660066&logo=MQTT&logoColor=FFFFFF&label=
[ow-api-url]: https://openweathermap.org/current
[ow-url]: https://openweathermap.org/
[prometheus-url]: https://prometheus.io/
[python-url]: https://www.python.org/
[python-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Python&color=3776AB&logo=Python&logoColor=FFFFFF&label=
[sentry-url]: https://sentry.io/
[sentry-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Sentry&color=362D59&logo=Sentry&logoColor=FFFFFF&label=
[switch-app-url]: https://github.com/tailucas/switch-app
[zmq-url]: https://zeromq.org/
[zmq-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=ZeroMQ&color=DF0000&logo=ZeroMQ&logoColor=FFFFFF&label=
[blog-url]: https://tailucas.github.io/update/2023/06/04/inverter-monitoring.html
