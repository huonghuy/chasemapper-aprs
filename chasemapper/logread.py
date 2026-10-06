#!/usr/bin/env python
#
#   Project Horus - Log read operations
#
#   Copyright (C) 2019  Mark Jessop <vk5qi@rfhead.net>
#   Released under GNU GPL v3 or later
#
import json
import logging
import os

from dateutil.parser import parse


def read_file(filename):
    """ Read log file, and output an array of dicts. """
    _output = []

    with open(filename, "r") as _f:
        for _line in _f:
            try:
                _data = json.loads(_line)
                _output.append(_data)
            except Exception as e:
                logging.debug("Error reading line: %s" % str(e))
    if len(_output) != 0:
        logging.info("Read %d log entries from %s" % (len(_output), filename))

    return _output


def read_last_balloon_telemetry(log_dir="./log_files"):
    """ Return the last balloon telemetry entry from the newest log file that has any. """
    for _name in sorted(os.listdir(log_dir), reverse=True):
        if not _name.endswith(".log"):
            continue
        try:
            _log = read_file(os.path.join(log_dir, _name))
        except Exception as e:
            logging.debug("Error reading file - maybe in use: %s" % str(e))
            continue

        _telemetry = [_e for _e in _log if _e.get("log_type") == "BALLOON TELEMETRY"]
        if _telemetry:
            _last = _telemetry[-1]
            _last["time_dt"] = parse(_last.pop("time"))
            return _last
