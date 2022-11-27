FROM debian:buster
ENV INITSYSTEM on
ENV container docker

LABEL Description="inverter_monitor" Vendor="tglucas" Version="1.0"

ENV DEBIAN_FRONTEND noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN true

RUN apt-get clean && apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    dbus \
    html-xml-utils \
    htop \
    jq \
    less \
    lsof \
    libffi-dev \
    # for rust build of Python cryptography
    libssl-dev \
    patch \
    procps \
    python3-certifi \
    python3-dbus \
    python3 \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-venv \
    python3-wheel \
    rsyslog \
    strace \
    sqlite3 \
    tree \
    unzip \
    vim \
    wget

# python3 default
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1

# setup
WORKDIR /opt/app
COPY requirements.txt .
COPY pylib/requirements.txt ./pylib/requirements.txt
COPY app_setup.sh .
RUN /opt/app/app_setup.sh
# ngrok
COPY ngrok_setup.sh .
RUN /opt/app/ngrok_setup.sh

COPY config ./config
COPY entrypoint.sh .
COPY pylib ./pylib
COPY pylib/pylib ./lib
COPY inverter_monitor .

# ssh, http, zmq, ngrok
EXPOSE 22 5000 5556 5558 4040 8080
CMD ["/opt/app/entrypoint.sh"]
