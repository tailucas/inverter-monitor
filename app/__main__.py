#!/usr/bin/env python
import logging.handlers

import binascii
import builtins
import libscrc
import re
import requests
import simplejson as json
import socket
import threading
import time
import zmq

import paho.mqtt.client as mqtt
from paho.mqtt.client import MQTT_ERR_NO_CONN

from collections import deque
from pathlib import Path
from simplejson.scanner import JSONDecodeError
from zmq.error import ZMQError, ContextTerminated

import os.path

# setup builtins used by pylib init
from . import APP_NAME
builtins.SENTRY_EXTRAS = []
influx_creds_section = 'local'


class CredsConfig:
    sentry_dsn: f'opitem:"Sentry" opfield:{APP_NAME}.dsn' = None  # type: ignore
    cronitor_token: f'opitem:"cronitor" opfield:.password' = None  # type: ignore
    influxdb_org: f'opitem:"InfluxDB" opfield:{influx_creds_section}.org' = None  # type: ignore
    influxdb_token: f'opitem:"InfluxDB" opfield:{influx_creds_section}.token' = None  # type: ignore
    influxdb_url: f'opitem:"InfluxDB" opfield:{influx_creds_section}.url' = None  # type: ignore
    weather_api_key: f'opitem:"OpenWeather" opfield:.password' = None  # type: ignore


# instantiate class
builtins.creds_config = CredsConfig()

from tailucas_pylib import app_config, \
    creds, \
    device_name_base, \
    log

from tailucas_pylib.process import SignalHandler
from tailucas_pylib import threads
from tailucas_pylib.threads import thread_nanny, bye, die
from tailucas_pylib.app import AppThread
from tailucas_pylib.zmq import zmq_term, Closable
from tailucas_pylib.handler import exception_handler

from requests.adapters import ConnectionError
from requests.exceptions import RequestException

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import ASYNCHRONOUS


URL_WORKER_APP = 'inproc://app-worker'
URL_WORKER_MQTT_PUBLISH = 'inproc://mqtt-publish'


DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
ERROR_RETRY_INTERVAL_SECONDS = 5
IMPLAUSIBLE_CHANGE_PERCENTAGE = 5
BATTERY_LOW_PCT = 45
# assuming CFE drop-out at 30%
BATTERY_CRITICAL_PCT = 40
# idle small home ~ 300W
BATTERY_MAJOR_DRAW_W = 500


def twos_complement_hex(hexval):
    bits = 16
    val = int(hexval, bits)
    if val & (1 << (bits-1)):
        val -= 1 << bits
    return val


class LoggerReader(AppThread):

    def __init__(self, field_mappings, logger_sn, logger_ip, logger_port, sample_interval_secs=DEFAULT_SAMPLE_INTERVAL_SECONDS):
        AppThread.__init__(self, name=self.__class__.__name__)
        self.field_mappings = field_mappings
        self.logger_sn = logger_sn
        self.logger_ip = logger_ip
        self.logger_port = logger_port
        self.sample_interval_secs = sample_interval_secs

    def get_logger_data(self):
        output = {}

        client_socket = None
        pini = 59
        pfin = 112
        chunks = 0
        while chunks < 2:
            start = binascii.unhexlify('A5')  # start
            length = binascii.unhexlify('1700')  # datalength
            controlcode = binascii.unhexlify('1045')  # controlCode
            serial = binascii.unhexlify('0000')  # serial
            datafield = binascii.unhexlify('020000000000000000000000000000')  # com.igen.localmode.dy.instruction.send.SendDataField
            pos_ini = str(hex(pini)[2:4].zfill(4))
            pos_fin = str(hex(pfin-pini+1)[2:4].zfill(4))
            businessfield = binascii.unhexlify('0103' + pos_ini + pos_fin)  # sin CRC16MODBUS
            crc = binascii.unhexlify(str(hex(libscrc.modbus(businessfield))[4:6])+str(hex(libscrc.modbus(businessfield))[2:4]))  # CRC16modbus
            checksum = binascii.unhexlify('00')  # checksum F2
            endCode = binascii.unhexlify('15')

            inverter_sn2 = bytearray.fromhex(hex(self.logger_sn)[8:10] + hex(self.logger_sn)[6:8] + hex(self.logger_sn)[4:6] + hex(self.logger_sn)[2:4])
            frame = bytearray(start + length + controlcode + serial + inverter_sn2 + datafield + businessfield + crc + checksum + endCode)

            checksum = 0
            frame_bytes = bytearray(frame)
            for i in range(1, len(frame_bytes) - 2, 1):
                checksum += frame_bytes[i] & 255
            frame_bytes[len(frame_bytes) - 2] = int((checksum & 255))

            # OPEN SOCKET
            log.debug(f'Opening stream socket to logger {self.logger_sn} @ {self.logger_ip}:{self.logger_port}...')
            for res in socket.getaddrinfo(self.logger_ip, self.logger_port, socket.AF_INET, socket.SOCK_STREAM):
                family, socktype, proto, canonname, sockadress = res
                try:
                    client_socket = socket.socket(family, socktype, proto)
                    client_socket.settimeout(10)
                    client_socket.connect(sockadress)
                except socket.error as msg:
                    log.warning(f'{msg}')
                    return None

            # SEND DATA
            log.debug(f'Sending {len(frame_bytes)} bytes data frame for chunk {chunks}.')
            client_socket.sendall(frame_bytes)

            # RECEIVE RESPONSE
            data = None
            try:
                data = client_socket.recv(1024)
                if data is None:
                    log.warning(f'No response data.')
                    return None
            except socket.timeout as msg:
                log.warning(f'{msg}')
                return None
            finally:
                try:
                    client_socket.close()
                except socket.error as msg:
                    log.warning(f'{msg}')

            log.debug(f'Received {len(data)} bytes for chunk {chunks}.')
            # PARSE RESPONSE (start position 56, end position 60)
            totalpower = 0
            i = pfin - pini
            a = 0
            while a <= i:
                p1 = 56+(a*4)
                p2 = 60+(a*4)
                try:
                    response = twos_complement_hex(str(''.join(hex(ord(chr(x)))[2:].zfill(2) for x in bytearray(data))+'  '+re.sub('[^\x20-\x7f]', '', ''))[p1:p2])
                except ValueError:
                    log.warning(f'Discarding {len(data)} byte response.', exc_info=True)
                    return None
                hexpos = str("0x") + str(hex(a+pini)[2:].zfill(4)).upper()
                for parameter in self.field_mappings:
                    for item in parameter["items"]:
                        title = item["titleEN"]
                        ratio = item["ratio"]
                        unit = item["unit"]
                        for register in item["registers"]:
                            if register == hexpos and chunks != -1:
                                if title.find("Temperature") != -1:
                                    response = round(response * ratio-100, 2)
                                else:
                                    response = round(response * ratio, 2)
                                if len(unit) > 0:
                                    key = f'{title} {unit}'
                                else:
                                    key = f'{title}'
                                # sanitize string
                                key = key.replace(' ','_').replace('-','_').replace('º','c').replace('%','pct').lower()
                                output[key] = response
                                if hexpos == '0x00BA':
                                    totalpower += response * ratio
                                if hexpos == '0x00BB':
                                    totalpower += response * ratio
                a+=1
            pini=150
            pfin=195
            chunks+=1
        log.debug(f'Fetched {len(output)} fields after {chunks} chunks.')
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(f'Using inverter logger {self.logger_sn} at address {self.logger_ip}:{self.logger_port}.')
        with exception_handler(connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True) as app_socket:
            prev_battery_soc = None
            prev_battery_soc_set = time.time()
            while not threads.shutting_down:
                operation_start_time = time.time()
                tries = 1
                logger_data = None
                # try within the time budget to get a plausible value, relative to the previous
                while time.time() - operation_start_time < DEFAULT_SAMPLE_INTERVAL_SECONDS/2:
                    tries += 1
                    now = time.time()
                    logger_data = self.get_logger_data()
                    if isinstance(logger_data, dict):
                        if 'battery_soc_pct' in logger_data.keys():
                            battery_soc = logger_data['battery_soc_pct']
                            battery_voltage = logger_data['battery_voltage_v']
                            # implausible battery state
                            if battery_soc == 0 and battery_voltage == 0:
                                log.warning(f'{battery_soc=}% and {battery_voltage=}v. Treating this output as implausible: {str(logger_data)}')
                                continue
                            # no previous to compare
                            if prev_battery_soc is None:
                                prev_battery_soc = battery_soc
                                prev_battery_soc_set = now
                                # current dict is good enough, break the try loop
                                break
                            soc_delta_pct = int(battery_soc-prev_battery_soc)
                            prev_battery_soc_last_set = now - prev_battery_soc_set
                            log.debug(f'battery_soc_pct changed by {soc_delta_pct}% from {prev_battery_soc} (set {prev_battery_soc_last_set:.2f}s ago) to {battery_soc}.')
                            # check for an implausible negative change within some time bound
                            if abs(soc_delta_pct) >= IMPLAUSIBLE_CHANGE_PERCENTAGE and prev_battery_soc_last_set < DEFAULT_SAMPLE_INTERVAL_SECONDS*2:
                                log.warning(f'battery_soc_pct changed by more than {IMPLAUSIBLE_CHANGE_PERCENTAGE}% from {prev_battery_soc} to {battery_soc}. Treating this output as implausible: {str(logger_data)}')
                            else:
                                # accept the new value as good
                                prev_battery_soc = battery_soc
                                prev_battery_soc_set = now
                                # control field change is plausible
                                break
                    log.warning(f'Waiting {ERROR_RETRY_INTERVAL_SECONDS}s after {tries} unsuccessful tries.')
                    threads.interruptable_sleep.wait(ERROR_RETRY_INTERVAL_SECONDS)
                if logger_data is not None and len(logger_data) > 0:
                    log.debug(f'Sending {len(logger_data)} fields for publication.')
                    app_socket.send_pyobj({'inverter': logger_data})
                else:
                    log.warning(f'Unable to fetch any valid data after {tries} tries (within {DEFAULT_SAMPLE_INTERVAL_SECONDS}s).')
                # stop for the remainder of the sampling interval
                operation_time = time.time() - operation_start_time
                sample_delay = self.sample_interval_secs - operation_time
                if sample_delay < 0:
                    normalized_sample_delay = min(operation_time, self.sample_interval_secs)
                    log.warning(f'Sample interval of {self.sample_interval_secs}s is too short, implying wait of {sample_delay:.2f}s. Resetting delay to {normalized_sample_delay:.2f}s.')
                    # don't use 0: never spin
                    sample_delay = normalized_sample_delay
                log.debug(f'Waiting {sample_delay:.2f}s until the next sample.')
                threads.interruptable_sleep.wait(sample_delay)


class WeatherReader(AppThread):

    def __init__(self):
        AppThread.__init__(self, name=self.__class__.__name__)
        self.api_key = creds.weather_api_key
        self.lat, self.lon = tuple(app_config.get('weather', 'coord_lat_lon').split(','))

    def get_weather_data(self):
        output = None
        try:
            r = requests.get('https://api.openweathermap.org/data/2.5/weather', params={
                'lat': self.lat,
                'lon': self.lon,
                'appid': self.api_key,
            })
            try:
                output = json.loads(r.content)
                log.debug(f'Loaded {len(output)} weather fields.')
            except JSONDecodeError:
                log.warning(f'JSON parse error of {r.content}', exc_info=True)
                return None
        except (OSError, ConnectionError, RequestException):
            log.warning('Problem getting weather data.', exc_info=True)
            return None
        return output

    # noinspection PyBroadException
    def run(self):
        log.info(f'Fetching weather data using coordinates [{self.lat},{self.lon}].')
        with exception_handler(connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True) as app_socket:
            while not threads.shutting_down:
                wd = self.get_weather_data()
                log.debug(f'Received weather data: {wd}')
                if wd is not None and len(wd) > 0:
                    weather = dict()
                    weather['cloudiness_pct'] = wd['clouds']['all']
                    date_value = int(wd['dt'])
                    sunrise = int(wd['sys']['sunrise'])
                    sunset = int(wd['sys']['sunset'])
                    sun_output = 0
                    # calculate theoretical sun output
                    if date_value > sunrise and date_value < sunset:
                        # normalize and divide
                        midday_secs = (sunset - sunrise) / 2
                        secs_from_dark = min(date_value - sunrise, sunset - date_value)
                        sun_output = int((secs_from_dark / midday_secs) * 100)
                        log.debug(f'Derived {sun_output}% sun output from {sunrise=},{date_value=},{sunset=},{midday_secs=},{secs_from_dark=}')
                    else:
                        log.debug(f'Using {sun_output}% sun output from {sunrise=},{date_value=},{sunset=}')
                    country = wd['sys']['country']
                    weather['midday_pct'] = sun_output
                    log.debug(f'{country}: Sending {len(weather)} fields for publication: {weather}')
                    app_socket.send_pyobj({'weather': weather})
                threads.interruptable_sleep.wait(DEFAULT_SAMPLE_INTERVAL_SECONDS)


class MqttSubscriber(AppThread, Closable):

    def __init__(self, mqtt_server_address, mqtt_topic_prefix, mqtt_switch_devices):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_MQTT_PUBLISH)

        self._mqtt_client = None
        self._mqtt_server_address = mqtt_server_address
        self._mqtt_subscribe_topic_prefix = mqtt_topic_prefix
        self._mqtt_switch_devices = mqtt_switch_devices

        self._disconnected = False

        self._switch_state = dict()

        self._power_generation_history = deque(maxlen=5)

    def close(self):
        Closable.close(self)
        try:
            self._mqtt_client.disconnect()
        except Exception:
            log.warning('Ignoring error closing MQTT socket.', exc_info=True)

    def on_connect(self, client, userdata, flags, rc):
        subscription_topic = f'{self._mqtt_subscribe_topic_prefix}/state/#'
        log.info(f'Subscribing to topic [{subscription_topic}]...')
        self._mqtt_client.subscribe(subscription_topic)

    def on_disconnect(self, client, userdata, rc):
        log.info('MQTT client has disconnected.')
        self._disconnected = True

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload
        log.debug(f'{topic} received {len(payload)} bytes.')
        msg_data = None
        try:
            log.debug(f'{topic} received: {payload}')
            msg_data = json.loads(payload)
        except JSONDecodeError:
            log.exception(f'Unstructured message: {payload}')
            return
        except ContextTerminated:
            self.close()
        if 'switches' in msg_data.keys():
            switch_bank = topic.split('/')[2]
            new_state = msg_data['switches']
            old_state = list()
            if switch_bank in self._switch_state:
                old_state = self._switch_state[switch_bank]
            if new_state != old_state:
                for ids, s in enumerate(new_state):
                    log.info(f'[{switch_bank}] Switch {ids+1} is now in state [{s}]')
            # state capture
            self._switch_state[switch_bank] = new_state

    def set_switch_state(self, switch_state=1):
        for switch_bank in self._switch_state.keys():
            if switch_bank not in self._mqtt_switch_devices:
                log.warning(f'Not changing state for {switch_bank} due to missing configuration.')
                continue
            mqtt_pub_topic = '/'.join([
                f'{self._mqtt_subscribe_topic_prefix}',
                'control',
                switch_bank
            ])
            mqtt_update = list()
            for ids, _ in enumerate(self._switch_state[switch_bank]):
                mqtt_update.append(switch_state)
            message_data = json.dumps({'state': mqtt_update})
            log.debug(f'[{mqtt_pub_topic}] Publishing {len(message_data)} bytes: [{message_data}]')
            self._mqtt_client.publish(topic=mqtt_pub_topic, payload=message_data)

    def get_power_generation_avg(self, value):
        self._power_generation_history.append(value)
        total = 0
        for sample in self._power_generation_history:
            total += sample
        return total / len(self._power_generation_history)

    # noinspection PyBroadException
    def run(self):
        log.info(f'Connecting to MQTT server {self._mqtt_server_address}...')
        self._mqtt_client = mqtt.Client()
        self._mqtt_client.on_connect = self.on_connect
        self._mqtt_client.on_disconnect = self.on_disconnect
        self._mqtt_client.on_message = self.on_message
        self._mqtt_client.connect(self._mqtt_server_address)
        my_socket = self.get_socket()
        with exception_handler(connect_url=URL_WORKER_APP, and_raise=False, shutdown_on_error=True) as app_socket:
            prev_switch_state = 0
            while not threads.shutting_down:
                switch_stats = dict()
                rc = self._mqtt_client.loop()
                if rc == MQTT_ERR_NO_CONN or self._disconnected:
                    raise ResourceWarning(f'No connection to MQTT broker at {self._mqtt_server_address} (disconnected? {self._disconnected})')
                inverter_data = None
                # check for messages to publish
                try:
                    inverter_data = my_socket.recv_pyobj(flags=zmq.NOBLOCK)
                except ZMQError:
                    # ignore, no data
                    continue
                if not isinstance(inverter_data, dict):
                    continue
                # check for required fields
                if not all(field in inverter_data.keys() for field in ['alert', 'battery_power_w', 'pv1_power_w', 'pv2_power_w']):
                    continue
                switch_state = 1
                switch_stats['surplus_ration'] = 0
                switch_stats['battery_ration'] = 0
                if int(inverter_data['alert']) == 1:
                    # do not load shed during an alert condition
                    self.set_switch_state()
                    continue
                # check 1: calculate surplus as a function of PV reported *usage* and how much the batteries are supplying
                pv1_power_w = float(inverter_data['pv1_power_w'])
                pv2_power_w = float(inverter_data['pv2_power_w'])
                battery_power_w = float(inverter_data['battery_power_w'])
                power_generation_w_avg = self.get_power_generation_avg(value=pv1_power_w + pv2_power_w - battery_power_w)
                # disable switch if battery is critically low without adequate surplus (i.e. not charging from solar)
                battery_soc_pct = inverter_data['battery_soc_pct']
                if battery_soc_pct < BATTERY_CRITICAL_PCT and power_generation_w_avg < 0:
                    switch_state = 0
                    switch_stats['surplus_ration'] = 1
                # check 2: determine battery state of charge and whether there is any grid fallback
                grid_voltage_l1_v = float(inverter_data['grid_voltage_l1_v'])
                grid_voltage_l2_v = float(inverter_data['grid_voltage_l2_v'])
                grid_voltage = max(grid_voltage_l1_v, grid_voltage_l2_v)
                # more conservative rationing if there is no grid backup (draw assumes no surplus)
                if battery_soc_pct < BATTERY_LOW_PCT and grid_voltage < 90 and battery_power_w >= BATTERY_MAJOR_DRAW_W:
                    switch_state = 0
                    switch_stats['battery_ration'] = 1
                # check 3: determine whether the inverter is no longer pulling from solar or battery (i.e. from grid)
                inverter_l1_power_w = float(inverter_data['inverter_l1_power_w'])
                inverter_l2_power_w = float(inverter_data['inverter_l2_power_w'])
                # can't use min/max because l2 is normally 0
                inverter_power_w = inverter_l1_power_w + inverter_l2_power_w
                if inverter_power_w < 0:
                    switch_state = 0
                    switch_stats['battery_ration'] = 1
                # log the supporting data
                log_msg = (
                    f'Inverter is delivering {inverter_power_w}w to consumers from backup (solar/battery). '
                    f'Power surplus average is {power_generation_w_avg:.2f}w ({pv1_power_w=:.2f}w, {pv2_power_w=:.2f}w, {battery_power_w=:.2f}w). '
                    f'Battery discharge {battery_power_w}w with remaining charge of {battery_soc_pct}% and supporting grid voltage of {grid_voltage}v. '
                    f'Updating switch banks to [{switch_state}].'
                )
                if prev_switch_state != switch_state:
                    log.info(log_msg)
                elif log.level == logging.DEBUG:
                    log.debug(log_msg)
                # update switches
                self.set_switch_state(switch_state=switch_state)
                prev_switch_state = switch_state
                # post stats
                switch_stats['switch_state'] = switch_state
                app_socket.send_pyobj({'switches': switch_stats})
                # for other interested consumers
                self._mqtt_client.publish(topic='inverter/state', payload=json.dumps(inverter_data))
        self.close()


class EventProcessor(AppThread, Closable):

    def __init__(self):
        AppThread.__init__(self, name=self.__class__.__name__)
        Closable.__init__(self, connect_url=URL_WORKER_APP)

        self.influxdb_bucket = app_config.get('influxdb', 'bucket')

        self.influxdb = None
        self.influxdb_rw = None
        self.influxdb_ro = None

    def _influxdb_write(self, point_name, field_name, field_value):
        try:
            self.influxdb_rw.write(
                bucket=self.influxdb_bucket,
                record=Point(point_name).tag("application", APP_NAME).tag("device", device_name_base).field(field_name, field_value))
        except Exception:
            log.warning(f'Unable to post to InfluxDB.', exc_info=True)

    # noinspection PyBroadException
    def run(self):
        # influx DB
        log.info(f'Connecting to InfluxDB at {creds.influxdb_url} using bucket {self.influxdb_bucket}.')
        self.influxdb = InfluxDBClient(
            url=creds.influxdb_url,
            token=creds.influxdb_token,
            org=creds.influxdb_org)
        self.influxdb_rw = self.influxdb.write_api(write_options=ASYNCHRONOUS)
        self.influxdb_ro = self.influxdb.query_api()
        my_socket = self.get_socket()
        with exception_handler(connect_url=URL_WORKER_MQTT_PUBLISH, and_raise=False, shutdown_on_error=True) as mqtt_socket:
            while not threads.shutting_down:
                event = my_socket.recv_pyobj()
                log.debug(event)
                if isinstance(event, dict):
                    for point_name in list(event):
                        point_items = event[point_name]
                        for key, value in point_items.items():
                            self._influxdb_write(point_name, key, value)
                        log.debug(f'Wrote {len(point_items)} {point_name} points.')
                        if point_name == 'inverter':
                            mqtt_socket.send_pyobj(point_items)
        self.close()


def main():
    log.setLevel(logging.INFO)
    # load basic configuration
    mappings = None
    mappings_file = ''.join([os.path.join(APP_PATH, 'config', 'field_mappings.txt')])  # type: ignore
    with open(mappings_file) as mapping_file:
        try:
            mappings = json.loads(mapping_file.read())
            log.info(f'Loaded {len(mappings)} field mappings from {mappings_file}')
        except JSONDecodeError as e:
            log.exception(f'Error loading {mappings_file}.')
            raise e
    # ensure proper signal handling; must be main thread
    signal_handler = SignalHandler()
    event_processor = EventProcessor()
    logger_reader = LoggerReader(
        field_mappings=mappings,
        logger_sn=app_config.getint('inverter', 'logger_sn'),
        logger_ip=app_config.get('inverter', 'logger_address'),
        logger_port=app_config.getint('inverter', 'logger_port'),
        sample_interval_secs=app_config.getint('inverter', 'logger_sample_interval_seconds'))
    weather_reader = WeatherReader()
    mqtt_subscriber = MqttSubscriber(
        mqtt_server_address=app_config.get('mqtt', 'server_address'),
        mqtt_topic_prefix=app_config.get('mqtt', 'topic_prefix'),
        mqtt_switch_devices=app_config.get('mqtt', 'switch_device_csv').split(','))
    nanny = threading.Thread(
        name='nanny',
        target=thread_nanny,
        args=(signal_handler,),
        daemon=True)
    # startup completed
    # back to INFO logging
    log.setLevel(logging.INFO)
    try:
        log.info(f'Starting {APP_NAME} threads...')
        event_processor.start()
        logger_reader.start()
        weather_reader.start()
        mqtt_subscriber.start()
        # start thread nanny
        nanny.start()
        log.info('Startup complete.')
        # hang around until something goes wrong
        threads.interruptable_sleep.wait()
        raise RuntimeWarning("Shutting down...")
    except(KeyboardInterrupt, RuntimeWarning, ContextTerminated) as e:
        die()
    finally:
        zmq_term()
    bye()


if __name__ == "__main__":
    main()