#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#
#   SPOT GPS Tracker public XML feed poller.
#
#   Polls findmespot.com public feeds for one or more trackers and forwards
#   positions through the same summary_callback used by APRS-IS / Horus UDP,
#   so SPOT traces appear on the map as additional balloon tracks for
#   cross-referencing.
#
#   Feed IDs are read from environment variables (mirroring RECOVERY_API_KEY)
#   to keep them out of the committed config.
#


import datetime
import logging
import os
import threading
import time

import requests
from defusedxml import ElementTree as ET


SPOT_FEED_URL = (
    "https://api.findmespot.com/spot-main-web/consumer/rest-api/2.0/public/feed/"
    "{feed_id}/message.xml"
)

# SPOT's public API rate-limits at roughly 1 request / 2.5 minutes (150 s)
# per feed, so clamp the poll interval to that. The recommended default is
# 300 s (5 min) — see spot_poll_interval in horusmapper.cfg.example.
MIN_POLL_INTERVAL = 150


class SPOTListener:
    """Polls one or more SPOT public XML feeds and emits positions via summary_callback.

    feeds: list of (callsign, env_var_name) tuples. Feed IDs are pulled from
    os.environ[env_var_name] at startup; missing / empty vars are skipped with
    a warning.
    """

    def __init__(self, feeds, summary_callback, poll_interval=300):
        self.summary_callback = summary_callback
        self.poll_interval = max(int(poll_interval), MIN_POLL_INTERVAL)

        self._feeds = []
        for callsign, env_var in feeds:
            feed_id = os.environ.get(env_var, "").strip()
            if not feed_id:
                logging.warning(
                    "SPOT: %s skipped — env var %s is not set", callsign, env_var
                )
                continue
            self._feeds.append({
                "callsign": callsign,
                "feed_id": feed_id,
                "last_unix_time": 0,
            })

        self._running = False
        self._thread = None

    def start(self):
        if not self._feeds:
            logging.info("SPOT: no feeds configured, not starting listener.")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logging.info(
            "SPOT: started listener for %d feed(s), poll interval %ds",
            len(self._feeds),
            self.poll_interval,
        )

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        # Poll once immediately, then on the interval.
        while self._running:
            for feed in self._feeds:
                if not self._running:
                    break
                try:
                    self._poll_feed(feed)
                except Exception as e:
                    logging.error(
                        "SPOT: error polling %s: %s", feed["callsign"], e
                    )

            # Sleep in 1-second chunks so close() returns promptly.
            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_feed(self, feed):
        url = SPOT_FEED_URL.format(feed_id=feed["feed_id"])
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logging.warning(
                "SPOT: %s returned HTTP %d", feed["callsign"], resp.status_code
            )
            return

        try:
            root = ET.fromstring(resp.content)
        except Exception as e:
            logging.error("SPOT: %s XML parse error: %s", feed["callsign"], e)
            return

        # SPOT can return an error envelope (e.g. rate-limited, invalid feed).
        errors = root.findall(".//error")
        if errors:
            for err in errors:
                code = err.findtext("code", "")
                text = err.findtext("text", "")
                logging.warning(
                    "SPOT: %s API error %s: %s", feed["callsign"], code, text
                )
            return

        messages = root.findall(".//message")
        if not messages:
            logging.debug("SPOT: %s no messages in feed", feed["callsign"])
            return

        # SPOT returns newest-first; sort oldest-first so we replay history
        # in chronological order on first poll and draw a sensible track.
        parsed = []
        for msg in messages:
            try:
                ut = int(msg.findtext("unixTime"))
                lat = float(msg.findtext("latitude"))
                lon = float(msg.findtext("longitude"))
            except (TypeError, ValueError):
                continue
            alt_text = msg.findtext("altitude")
            try:
                alt = int(float(alt_text)) if alt_text else 0
            except ValueError:
                alt = 0
            parsed.append((ut, lat, lon, alt))

        parsed.sort(key=lambda m: m[0])

        new_count = 0
        for ut, lat, lon, alt in parsed:
            if ut <= feed["last_unix_time"]:
                continue
            feed["last_unix_time"] = ut
            new_count += 1
            packet_time = datetime.datetime.fromtimestamp(ut, tz=datetime.timezone.utc).strftime("%H:%M:%S")
            try:
                self.summary_callback({
                    "type": "PAYLOAD_SUMMARY",
                    "callsign": feed["callsign"],
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": alt,
                    "speed": -1,
                    "heading": -1,
                    "time": packet_time,
                    "comment": "SPOT",
                })
            except Exception as e:
                logging.error(
                    "SPOT: %s summary_callback error: %s", feed["callsign"], e
                )

        if new_count:
            logging.info(
                "SPOT: %s emitted %d new position(s)", feed["callsign"], new_count
            )
