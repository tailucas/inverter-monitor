#!/bin/bash
set -eu
set -o pipefail
set -x

# Run user
export APP_USER="${APP_USER:-app}"
export APP_GROUP="${APP_GROUP:-app}"

# groups
groupadd -f -r "${APP_GROUP}"
# non-root users
id -u "${APP_USER}" || useradd -r -g "${APP_GROUP}" "${APP_USER}"

TZ_CACHE=/data/localtime
# a valid symlink
if [ -h "$TZ_CACHE" ] && [ -e "$TZ_CACHE" ]; then
  cp -a "$TZ_CACHE" /etc/localtime
fi
# set the timezone
(tzupdate && cp -a /etc/localtime "$TZ_CACHE") || [ -e "$TZ_CACHE" ]

# application configuration (no tee for secrets)
cat /opt/app/config/app.conf | /opt/app/pylib/config_interpol > "/opt/app/${APP_NAME}.conf"
cat /opt/app/config/supervisord.conf | /opt/app/pylib/config_interpol > /opt/app/supervisord.conf

# so app user can make the noise
adduser "${APP_USER}" audio
chown "${APP_USER}:${APP_GROUP}" /opt/app/*
# non-volatile storage
chown -R "${APP_USER}:${APP_GROUP}" /data/
# logging
chown "${APP_USER}" /var/log/
# home
mkdir -p "/home/${APP_USER}/.aws/"
chown -R "${APP_USER}:${APP_GROUP}" "/home/${APP_USER}/"
# AWS configuration (no tee for secrets)
cat /opt/app/config/aws-config | /opt/app/pylib/config_interpol > "/home/${APP_USER}/.aws/config"
# patch botoflow to work-around
# AttributeError: 'Endpoint' object has no attribute 'timeout'
PY_BASE_WORKER="$(find /opt/app/ -name base_worker.py)"
patch -f -u "$PY_BASE_WORKER" -i /opt/app/config/base_worker.patch || true

# Bash history
echo "export HISTFILE=/data/.bash_history" >> /etc/bash.bashrc

env > /etc/environment
echo "HOME=/data/" >> /etc/environment
echo "USER=${APP_USER}" >> /etc/environment

# replace this entrypoint with supervisord
exec env /usr/local/bin/supervisord -n -c /opt/app/supervisord.conf
