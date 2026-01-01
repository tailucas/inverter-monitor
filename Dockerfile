FROM tailucas/base-app:latest
# for system/site packages
USER root
ARG DEBIAN_FRONTEND=noninteractive
# system setup
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        html-xml-utils \
        sqlite3 \
        wget \
    && rm -rf /var/lib/apt/lists/*
# generate correct locales
ARG LANG
ARG LANGUAGE
RUN locale-gen ${LANGUAGE} \
    && locale-gen ${LANG} \
    && update-locale \
    && locale -a
# cron jobs
RUN rm -f ./config/cron/base_job
# apply override
RUN "${APP_DIR}/app_setup.sh"
# add the project application
COPY app/__main__.py ./app/
# override configuration
COPY config/app.conf ./config/app.conf
COPY config/field_mappings.txt ./config/field_mappings.txt
# Python
COPY app ./app
COPY pyproject.toml uv.lock ./
RUN chown app:app uv.lock
# switch to run user now because uv does not use the environment to infer
USER app
RUN "${APP_DIR}/python_setup.sh"
# override entrypoint
COPY app_entrypoint.sh .
CMD ["/opt/app/entrypoint.sh"]
