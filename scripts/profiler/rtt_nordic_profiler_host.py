#
# Copyright (c) 2018 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: LicenseRef-Nordic-5-Clause

from pynrfjprog.LowLevel import API
from pynrfjprog.APIError import APIError
import time
import sys
from enum import Enum
from rtt_nordic_config import RttNordicConfig
from events import Event, EventType, EventsData
import logging

class Command(Enum):
    START = 1
    STOP = 2
    INFO = 3


class RttNordicProfilerHost:

    def __init__(self, config=RttNordicConfig, finish_event=None,
                 queue=None, event_filename=None,
                 event_types_filename=None, log_lvl=logging.WARNING):
        self.event_filename = event_filename
        self.event_types_filename = event_types_filename
        self.config = config
        self.finish_event = finish_event
        self.queue = queue
        self.received_events = EventsData([], {})
        self.timestamp_overflows = 0
        self.after_half = False

        self.desc_buf = ""
        self.bufs = list()
        self.bcnt = 0
        self.last_read_time = time.time()
        self.reading_data = True

        self.logger = logging.getLogger('RTT Profiler Host')
        self.logger_console = logging.StreamHandler()
        self.logger.setLevel(log_lvl)
        self.log_format = logging.Formatter('[%(levelname)s] %(name)s: %(message)s')
        self.logger_console.setFormatter(self.log_format)
        self.logger.addHandler(self.logger_console)

        self.rtt_up_channels = {
            'info': None,
            'data': None,
        }
        self.rtt_down_channels = {
            'command': None,
        }
        self.connect()

    @staticmethod
    def rtt_get_device_family(snr):
        family = None
        with API('UNKNOWN') as api:
            if snr is not None:
                api.connect_to_emu_with_snr(snr)
            else:
                api.connect_to_emu_without_snr()
            family = api.read_device_family()
            api.disconnect_from_emu()
        return family

    def connect(self):
        snr = self.config['device_snr']
        device_family = RttNordicProfilerHost.rtt_get_device_family(snr)
        self.logger.info('Recognized device family: ' + device_family)
        self.jlink = API(device_family)
        self.jlink.open()

        if snr is not None:
            self.jlink.connect_to_emu_with_snr(self.config['device_snr'])
        else:
            self.jlink.connect_to_emu_without_snr()

        if self.config['reset_on_start']:
            self.jlink.sys_reset()
            self.jlink.go()

        self.jlink.rtt_start()

        TIMEOUT = 20
        start_time = time.time()
        while not self.jlink.rtt_is_control_block_found():
            if time.time() - start_time > TIMEOUT:
                self.logger.error("Cannot find RTT control block")
                sys.exit()

            time.sleep(0.2)

        while (None in list(self.rtt_up_channels.values())) or \
              (None in list(self.rtt_down_channels.values())):
            down_channel_cnt, up_channel_cnt = self.jlink.rtt_read_channel_count()

            for idx in range(0, down_channel_cnt):
                chan_name, _ = self.jlink.rtt_read_channel_info(idx, 'DOWN_DIRECTION')

                try:
                    label = self.config['rtt_down_channel_names'][chan_name]
                    self.rtt_down_channels[label] = idx
                except KeyError:
                    continue

            for idx in range(0, up_channel_cnt):
                chan_name, _ = self.jlink.rtt_read_channel_info(idx, 'UP_DIRECTION')

                try:
                    label = self.config['rtt_up_channel_names'][chan_name]
                    self.rtt_up_channels[label] = idx
                except KeyError:
                    continue

            if time.time() - start_time > TIMEOUT:
                self.logger.error("Cannot find properly configured RTT channels")
                sys.exit()

            time.sleep(0.2)

        self.logger.info("Connected to device via RTT")

    def shutdown(self):
        self.disconnect()
        self._read_remaining_events()
        if self.event_filename and self.event_types_filename:
            self.received_events.write_data_to_files(self.event_filename,
                                                     self.event_types_filename)

    def disconnect(self):
        self.stop_logging_events()
        # read remaining data to buffer
        while True:
            try:
                buf = self.jlink.rtt_read(self.rtt_up_channels['data'],
                                          self.config['rtt_read_chunk_size'],
                                          encoding=None)

            except APIError:
                self.logger.error("Problem with reading RTT data.")
                buf = []

            if len(buf) > 0:
                self.bufs.append(buf)
                self.bcnt += len(buf)
            else:
                break

        try:
            self.jlink.rtt_stop()
            self.jlink.disconnect_from_emu()
            self.jlink.close()

        except APIError:
            self.logger.error("JLink connection lost. Saving collected data.")
            return

        self.logger.info("Disconnected from device")

    def _get_buffered_data(self, num_bytes):
        buf = bytearray()
        while len(buf) < num_bytes:
            tbuf = self.bufs[0]
            size = num_bytes - len(buf)
            if len(tbuf) <= size:
                buf = buf + tbuf
                del self.bufs[0]
            else:
                buf = buf + tbuf[0:size]
                self.bufs[0] = tbuf[size:]
        self.bcnt -= num_bytes
        return buf

    def _read_bytes(self, num_bytes):
        now = time.time()

        while self.reading_data:
            if now - self.last_read_time < self.config['rtt_read_period'] \
            and self.bcnt >= num_bytes:
                break

            try:
                buf = self.jlink.rtt_read(self.rtt_up_channels['data'],
                                          self.config['rtt_read_chunk_size'],
                                          encoding=None)
            except APIError:
                self.logger.error("Problem with reading RTT data.")
                self.shutdown()
                sys.exit()

            if len(buf) > 0:
                self.bufs.append(buf)
                self.bcnt += len(buf)

            if len(buf) > self.config['rtt_additional_read_thresh']:
                continue

            self.last_read_time = now

            if self.bcnt >= num_bytes:
                break

            if self.finish_event is not None and self.finish_event.is_set():
                self.finish_event.clear()
                self.logger.info("Real time transmission closed")
                self.shutdown()
                self.logger.info("Events data saved to files")
                sys.exit()

            time.sleep(0.05)

        return self._get_buffered_data(num_bytes)

    def _calculate_timestamp_from_clock_ticks(self, clock_ticks):
        return self.config['ms_per_timestamp_tick'] * (
            clock_ticks + self.timestamp_overflows * self.config['timestamp_raw_max']) / 1000

    def _read_single_event_description(self):
        while '\n' not in self.desc_buf:
            try:
                buf_temp = self.jlink.rtt_read(self.rtt_up_channels['info'],
                                      self.config['rtt_read_chunk_size'],
                                      encoding='utf-8')

            except APIError:
                self.logger.error("Problem with reading RTT data.")
                self.shutdown()

            self.desc_buf += buf_temp
            time.sleep(0.1)

        desc = str(self.desc_buf[0:self.desc_buf.find('\n')])
        # Empty field is send after last event description
        if len(desc) == 0:
            return None, None
        self.desc_buf = self.desc_buf[self.desc_buf.find('\n')+1:]

        desc_fields = desc.split(',')

        name = desc_fields[0]
        id = int(desc_fields[1])
        data_type = []
        for i in range(2, len(desc_fields) // 2 + 1):
            data_type.append(desc_fields[i])
        data = []
        for i in range(len(desc_fields) // 2 + 1, len(desc_fields)):
            data.append(desc_fields[i])
        return id, EventType(name, data_type, data)

    def _read_all_events_descriptions(self):
        while True:
            id, et = self._read_single_event_description()
            if (id is None or et is None):
                break
            self.received_events.registered_events_types[id] = et

    def get_events_descriptions(self):
        self._send_command(Command.INFO)
        self._read_all_events_descriptions()
        if self.queue is not None:
            self.queue.put(self.received_events.registered_events_types)
        self.logger.info("Received events descriptions")
        self.logger.info("Ready to start logging events")

    def _read_single_event_rtt(self):
        id = int.from_bytes(
            self._read_bytes(1),
            byteorder=self.config['byteorder'],
            signed=False)
        et = self.received_events.registered_events_types[id]

        buf = self._read_bytes(4)
        timestamp_raw = (
            int.from_bytes(
                buf,
                byteorder=self.config['byteorder'],
                signed=False))

        if self.after_half \
        and timestamp_raw < 0.2 * self.config['timestamp_raw_max']:
            self.timestamp_overflows += 1
            self.after_half = False

        if timestamp_raw > 0.6 * self.config['timestamp_raw_max']:
            if timestamp_raw < 0.9 * self.config['timestamp_raw_max']:
                self.after_half = True
        timestamp = self._calculate_timestamp_from_clock_ticks(timestamp_raw)

        def process_int32(self, data):
            buf = self._read_bytes(4)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=True))

        def process_uint32(self, data):
            buf = self._read_bytes(4)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=False))

        def process_int16(self, data):
            buf = self._read_bytes(2)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=True))

        def process_uint16(self, data):
            buf = self._read_bytes(2)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=False))

        def process_int8(self, data):
            buf = self._read_bytes(1)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=True))

        def process_uint8(self, data):
            buf = self._read_bytes(1)
            data.append(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                       signed=False))

        def process_string(self, data):
            buf = self._read_bytes(1)
            buf = self._read_bytes(int.from_bytes(buf, byteorder=self.config['byteorder'],
                                                  signed=False))
            data.append(buf.decode())

        READ_BYTES = {
            "u8": process_uint8,
            "s8": process_int8,
            "u16": process_uint16,
            "s16": process_int16,
            "u32": process_uint32,
            "s32": process_int32,
            "s": process_string,
            "t": process_uint32
        }
        data=[]
        for event_data_type in et.data_types:
            READ_BYTES[event_data_type](self, data)
        return Event(id, timestamp, data)

    def _read_remaining_events(self):
        self.reading_data = False
        while self.bcnt != 0:
            event = self._read_single_event_rtt()
            self.received_events.events.append(event)
            if self.queue is not None:
                self.queue.put(event)

        # End of transmission
        if self.queue is not None:
            self.queue.put(None)

    def read_events_rtt(self, time_seconds):
        self.logger.info("Start logging events data")
        self.start_logging_events()
        start_time = time.time()
        current_time = start_time
        while current_time - start_time < time_seconds or time_seconds < 0:
            event = self._read_single_event_rtt()
            self.received_events.events.append(event)
            if self.queue is not None:
                self.queue.put(event)
            current_time = time.time()
        self.logger.info("Real time transmission closed")
        self.shutdown()
        self.logger.info("Events data saved to files")
        sys.exit()

    def start_logging_events(self):
        self._send_command(Command.START)

    def stop_logging_events(self):
        self._send_command(Command.STOP)

    def _send_command(self, command_type):
        command = bytearray(1)
        command[0] = command_type.value
        try:
            self.jlink.rtt_write(self.rtt_down_channels['command'], command, None)
        except APIError:
            self.logger.error("Problem with writing RTT data.")
