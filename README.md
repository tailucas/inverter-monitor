<a name="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

## About The Project

The project interfaces with a [Deye][deye-url] electricity inverter and posts telemetry to both InfluxDB and the chosen MQTT topic. You may find my [companion project][switch-app-url] useful which is designed to react to MQTT messages posted by this project. That project is based on the IoT framework [Mongoose OS][mongoose-os-url]. This application extends my own [boilerplate application][baseapp-url] hosted in [docker hub][baseapp-image-url] and takes its own git submodule dependency on my own [package][pylib-url].

For your convenience, a [sample InfluxDB dashboard][influxdb-dashboard-template] is included to get you started.

![Dashboard Left](/assets/inverter_dashboard_a.png)

![Dashboard Right](/assets/inverter_dashboard_b.png)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

Technologies that help make this package useful:

[![1Password][1p-shield]][1p-url]
[![InfluxDB][influxdb-shield]][influxdb-url]
[![Poetry][poetry-shield]][poetry-url]
[![Python][python-shield]][python-url]
[![MQTT][mqtt-shield]][mqtt-url]
[![Sentry][sentry-shield]][sentry-url]

Also:

* [Cronitor][cronitor-url]
* [Healthchecks][healthchecks-url]
* [OpenWeather][ow-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->
## Getting Started

Here is some detail about the intended use of this package.

### Prerequisites

Beyond the Python dependencies defined in the [Poetry configuration](pyproject.toml), the project carries hardcoded dependencies on [Sentry][sentry-url] and [1Password][1p-url] in order to function.

### Installation

0. :stop_sign: This project uses [1Password Secrets Automation][1p-url] to store both application key-value pairs as well as runtime secrets. It is assumed that the connect server containers are already running on your environment. If you do not want to use this, then you'll need to fork this package and make the changes as appropriate. It's actually very easy to set up, but note that 1Password is a paid product with a free-tier for secrets automation. Here is an example of how this looks for my application and the generation of the docker-compose.yml relies on this step. Your secrets automation vault must contain an entry called `ENV.inverter_monitor` with these keys:

* `DEVICE_NAME`: For naming the container. This project uses `inverter-monitor`.
* `APP_NAME`: Used for referencing the application's actual name for the logger. This project uses `inverter_monitor`.
* `MQTT_SERVER_ADDRESS`: IP address of a network-local MQTT broker to capture some useful telemetry and control for switches.
* `MQTT_TOPIC_PREFIX`: The prefix to be used to disambiguate messages from other sources. This project uses `switch`.
* `INVERTER_LOGGER_ADDRESS`: IP address of a network-local inverter logger device (assuming it's paired to the local network over WiFi).
* `INVERTER_LOGGER_PORT`: IP port number of the logger. This project uses `8899`.
* `INVERTER_LOGGER_SN`: Serial number of the logger device. This is usually visible as a sticker on the logger.
* `INFLUXDB_BUCKET`: The configured bucket in InfluxDB that records the telemetry. This project uses `inverter` and assumes that an InfluxDB user has been created with permissions to access this bucket.
* `INVERTER_LOGGER_SAMPLE_INTERVAL_SECS`: The sampling interval for the logger. This project uses `60`. The logger is slow and does not tolerate overly aggressive polls in that it stops responding or is unable to also post to the web service if you expect the inverter status to be visible online too.
* `WEATHER_COORD`: Numerical coordinates of the solar install in order to equate production with weather conditions. This implementation uses [OpenWeather APIs][ow-api-url].
* `MQTT_SWITCH_DEVICE_CSV`: The unique identifier of the device. See more in the [switch application][switch-app-url].
* `OP_CONNECT_SERVER`, `OP_CONNECT_TOKEN`, `OP_CONNECT_VAULT`: Used to specify the URL of the 1Password connect server with associated client token and Vault ID. See [1Password](https://developer.1password.com/docs/connect/get-started#step-1-set-up-a-secrets-automation-workflow) for more.
* `HC_PING_URL`: [Healthchecks][healthchecks-url] URL of this application's current health check status.
* `CRONITOR_MONITOR_KEY`: Token to enable additional health checks presented in [Cronitor][cronitor-url]. This tracks thread count and overall health.

With these configured, you are now able to build the application.

In addition to this, [additional runtime configuration](https://github.com/tailucas/inverter-monitor/blob/dcca894a35f9111935047986199ec47679430b4a/app/__main__.py#L31-L37) is used by the application, and also need to be contained within the secrets vault. With these configured, you are now able to run the application.

1. Clone the repo
   ```sh
   git clone https://github.com/tailucas/inverter-monitor.git
   ```
2. Verify that the git submodule is present.
   ```sh
   git submodule init
   git submodule update
   ```
4. Make the Docker runtime user and set directory permissions. :hand: Be sure to first review the Makefile contents for assumptions around user IDs for Docker.
   ```sh
   make user
   ```
5. Now generate the docker-compose.yml:
   ```sh
   make setup
   ```
6. And generate the Docker image:
   ```sh
   make build
   ```
7. If successful and the local environment is running the 1Password connect containers, run the application. For foreground:
   ```sh
   make run
   ```
   For background:
   ```sh
   make rund
   ```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE EXAMPLES -->
## Usage

Running the application will:

* Connect to the configured inverter logger.
* Connect to the weather service.
* Post associated telemetry to the configured InfluxDB time series database and the configured MQTT broker.
* Will make decisions about switching off consumers based on a crude heuristic.

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
[influxdb-dashboard-template]: https://github.com/tailucas/inverter-monitor/blob/master/influxdb_dashboard_sample.json

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
[poetry-url]: https://python-poetry.org/
[poetry-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Poetry&color=60A5FA&logo=Poetry&logoColor=FFFFFF&label=
[python-url]: https://www.python.org/
[python-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Python&color=3776AB&logo=Python&logoColor=FFFFFF&label=
[sentry-url]: https://sentry.io/
[sentry-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Sentry&color=362D59&logo=Sentry&logoColor=FFFFFF&label=
