FROM tailucas/base-app:latest
# for system/site packages
USER root
# generate correct locales
ARG LANG
ARG LANGUAGE
RUN locale-gen ${LANGUAGE} \
    && locale-gen ${LANG} \
    && update-locale \
    && locale -a
# system setup
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential
# cron jobs
RUN rm -f ./config/cron/base_job
# apply override
RUN /opt/app/app_setup.sh
# override application
# add the project application
COPY app/__main__.py ./app/
# override configuration
COPY config/app.conf ./config/app.conf
COPY config/field_mappings.txt ./config/field_mappings.txt
COPY poetry.lock pyproject.toml ./
RUN chown app:app poetry.lock
# switch to run user
USER app
RUN /opt/app/python_setup.sh
CMD ["/opt/app/entrypoint.sh"]
