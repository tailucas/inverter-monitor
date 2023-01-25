FROM tailucas/base-app:20230125
# for system/site packages
USER root
# system setup
# https://github.com/inter169/systs/blob/master/alpine/crond/README.md
RUN apk update \
    && apk upgrade \
    && apk --no-cache add \
        gcc \
        musl-dev
# override dependencies
COPY requirements.txt .
# apply override
ENV PYTHON_ADD_WHEEL 1
RUN /opt/app/app_setup.sh
# switch to user
USER app
# override configuration
COPY config/app.conf ./config/app.conf
COPY config/field_mappings.txt ./config/field_mappings.txt
# remove base_app
RUN rm -f /opt/app/base_app
# add the project application
COPY inverter_monitor .
CMD ["/opt/app/entrypoint.sh"]
