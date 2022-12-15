FROM tailucas/base-app:20221215
# override dependencies
COPY requirements.txt .
# apply override
RUN /opt/app/app_setup.sh
# override configuration
COPY config/app.conf ./config/app.conf
COPY config/field_mappings.txt ./config/field_mappings.txt
# remove base_app
RUN rm -f /opt/app/base_app
# add the project application
COPY inverter_monitor .

# ssh, http, zmq, ngrok
EXPOSE 22 5000 5556 5558 4040 8080
CMD ["/opt/app/entrypoint.sh"]
