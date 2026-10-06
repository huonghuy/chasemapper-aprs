#!/usr/bin/env python
#
#   Project Horus - Chase Logging
#
#   Copyright (C) 2019  Mark Jessop <vk5qi@rfhead.net>
#   Released under GNU GPL v3 or later
#
import datetime
import json
import logging
import os
import time
from queue import Queue
from threading import Thread, Lock


class ChaseLogger(object):
    """ Chase Data Logger Class.
        Log all chase data into a file as lines of JSON.
    """

    def __init__(self, filename=None, log_dir="./log_files"):

        if filename is not None:
            # Use user-supplied filename if provided
            self.filename = filename
        else:
            # Otherwise, create a filename based on the current time.
            self.filename = os.path.join(
                log_dir, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%MZ.log")
            )

        self.file_lock = Lock()

        # Input Queue.
        self.input_queue = Queue()

        # Open the file.
        try:
            self.f = open(self.filename, "a")
            logging.info("Logging - Opened log file %s." % self.filename)
        except Exception as e:
            logging.error("Chase Logger - Could not open log file - %s" % str(e))
            return

        # Start queue processing thread.
        self.input_processing_running = True
        self.log_process_thread = Thread(target=self.process_queue)
        self.log_process_thread.start()

    def _enqueue(self, data, log_type):
        """ Stamp an entry with its type and log time, and queue it for writing. """
        data["log_type"] = log_type
        data["log_time"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if self.input_processing_running:
            self.input_queue.put(data)
        else:
            logging.error("Chase Logger - Processing not running, discarding.")

    def add_car_position(self, data):
        """ Log a chase car position update: a dict with time (datetime), lat, lon, alt, comment. """
        data["time"] = data["time"].isoformat()
        self._enqueue(data, "CAR POSITION")

    def add_balloon_telemetry(self, data):
        """ Log balloon telemetry. time_dt (a datetime) is stored as an ISO string under time. """
        data["time"] = data.pop("time_dt").isoformat()
        self._enqueue(data, "BALLOON TELEMETRY")

    def add_balloon_prediction(self, data):
        """ Log a prediction run """
        self._enqueue(data, "PREDICTION")

    def add_bearing(self, data):
        """ Log a packet of bearing data """
        self._enqueue(data, "BEARING")

    def process_queue(self):
        """ Process data from the input queue, and write telemetry to log files.
        """
        logging.info("Chase Logger - Started Chase Logger Thread.")

        while self.input_processing_running:

            # Process everything in the queue.
            self.file_lock.acquire()
            _wrote = False
            while self.input_queue.qsize() > 0:
                try:
                    _data = self.input_queue.get_nowait()
                    _data_str = json.dumps(_data)
                    self.f.write(_data_str + "\n")
                    _wrote = True
                except Exception as e:
                    logging.error("Chase Logger - Error processing data - %s" % str(e))

            # Flush each batch to disk. Without this, entries sit in the
            # stdio buffer until it fills (8 kB) or the file is closed,
            # so anything reading the log back while chasemapper is still
            # running - the KML export, read_last_balloon_telemetry() -
            # would miss the most recent packets.
            if _wrote:
                try:
                    self.f.flush()
                except Exception as e:
                    logging.error("Chase Logger - Error flushing log file - %s" % str(e))

            self.file_lock.release()
            # Sleep while waiting for some new data.
            time.sleep(5)

    def running(self):
        """ Check if the logging thread is running. 

        Returns:
            bool: True if the logging thread is running.
        """
        return self.input_processing_running

    def close(self):
        try:
            self.input_processing_running = False
            self.f.close()
        except Exception as e:
            logging.error("Chase Logger - Error when closing - %s" % str(e))

        logging.info("Chase Logger - Stopped Telemetry Logger Thread.")
