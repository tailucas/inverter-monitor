FROM tailucas/base-app:20230831
# for system/site packages
USER root
# generate correct locales
ARG LANG
ENV LANG=$LANG
ARG LANGUAGE
ENV LANGUAGE=$LANGUAGE
ARG LC_ALL
ENV LC_ALL=$LC_ALL
ARG ENCODING
ENV ENCODING=$ENCODING
RUN sed -i -e "s/# ${LANG} ${ENCODING}/${LANG} ${ENCODING}/" /etc/locale.gen && \
    dpkg-reconfigure --frontend=noninteractive locales && \
    update-locale LANG=${LANG} && locale
# cron jobs
RUN rm -f ./config/cron/base_job
# apply override
RUN /opt/app/app_setup.sh
# switch to user
USER app
# override configuration
COPY config/app.conf ./config/app.conf
COPY config/field_mappings.txt ./config/field_mappings.txt
COPY poetry.lock pyproject.toml ./
RUN /opt/app/python_setup.sh
# add the project application
COPY app/__main__.py ./app/
# apply override
CMD ["/opt/app/entrypoint.sh"]
