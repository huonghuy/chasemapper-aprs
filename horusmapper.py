#!/usr/bin/env python2.7
#
#   Project Horus - Browser-Based Chase Mapper
#
#   Copyright (C) 2018  Mark Jessop <vk5qi@rfhead.net>
#   Released under GNU GPL v3 or later
#
import functools
import sys

# Version check.
if sys.version_info < (3, 6):
    print("CRITICAL - chasemapper requires Python 3.6 or newer!")
    sys.exit(1)

import hmac
import json
import logging
import math
import warnings
import flask
from flask_socketio import SocketIO
import os.path
import pytz
import time
import traceback
from threading import Thread
from datetime import datetime, timedelta, timezone
UTC = timezone.utc
from dateutil.parser import parse

# Suppress noise from third-party libraries we don't control:
#  - aprslib: unfixed invalid escape sequence in a regex literal
#  - cusfpredict: still uses datetime.utcnow() (deprecated in Python 3.12+)
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"aprslib(\..*)?")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"cusfpredict(\..*)?")

from chasemapper import __version__ as CHASEMAPPER_VERSION
from chasemapper.config import *
from chasemapper.earthmaths import *
from chasemapper.geometry import *
from chasemapper.gps import SerialGPS
from chasemapper.gpsd import GPSDAdaptor
from chasemapper.aprsis import APRSISListener
from chasemapper.spot import SPOTListener
from chasemapper.atmosphere import time_to_landing
from chasemapper.listeners import OziListener, UDPListener, fix_datetime
from chasemapper.predictor import predictor_spawn_download, model_download_running
from chasemapper.habitat import (
    HabitatChaseUploader,
    initListenerCallsign,
    uploadListenerPosition,
)
from chasemapper.sondehub import SondehubChaseUploader
from chasemapper.logger import ChaseLogger
from chasemapper.logread import read_last_balloon_telemetry
from chasemapper.bearings import Bearings
from chasemapper.tawhiri import get_tawhiri_prediction
from chasemapper import airspace_cache, parcel_proxy, car_track_cache
from chasemapper import geofence as geofence_mod


# Define Flask Application, and allow automatic reloading of templates for dev work
app = flask.Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
# Reject oversized request bodies before route handlers buffer them.
# Largest legitimate upload is a geofence KML (capped at 5 MB in the route).
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# SocketIO instance
socketio = SocketIO(app)


# Chase Logger Instance (Initialised in main)
chase_logger = None

# Global stores of data.

# These settings are shared between server and all clients, and are updated dynamically.
chasemapper_config = {}

# Per-profile geofence storage. Populated at startup from a sidecar
# JSON file next to the chasemapper config; mutated by the
# /geofence/<profile> upload routes.
geofence_store = {}
geofence_store_path = ""

# Pointers to objects containing data listeners.
# These should all present a .close() function which will be called on
# listener profile change, or program exit.
data_listeners = []

# These settings are not editable by the client!
pred_settings = {}

# Offline map settings, again, not editable by the client.
map_settings = {"tile_server_enabled": False}

# KML overlay settings, not editable by the client.
kml_overlay_settings = {}

# Payload data Stores
current_payloads = {}  #  Archive data which will be passed to the web client
current_payload_tracks = (
    {}
)  # Store of payload Track objects which are used to calculate instantaneous parameters.

# Chase car position
car_track = GenericTrack()

# Bearing store
bearing_store = None
bearing_mode = False # Flag to indicate if we are receiving bearings

# Habitat/Sondehub Chase-Car uploader object
online_uploader = None

# APRS-IS listener instance (set when car_source_type = aprsis)
aprsis_listener = None

# Runtime-only launch preview state. A profile is locked after its first real
# balloon prediction so the car-origin preview cannot replace live tracking.
launch_preview_active_profile = None
launch_preview_locked_profiles = set()

# Copy out any extra fields from incoming telemetry that we want to pass on to the GUI.
# At the moment we're really only using the burst timer field.
EXTRA_FIELDS = ["bt", "temp", "humidity", "sats", "snr"]


#
#   Flask Routes
#


@app.route("/")
def flask_index():
    """ Render main index page """
    return flask.render_template("index.html")

@app.route("/bearing")
def flask_bearing_entry():
    """ Render bearing entry page """
    return flask.render_template("bearing_entry.html")

@app.route("/oclock")
def flask_oclock():
    """ Render bearing o'clock page """
    return flask.render_template("oclock.html")


def split_kml_overlay_settings(overlays):
    """ Split configured KML overlays into public config and private paths. """
    _public_overlays = []
    _overlay_settings = {}

    for _overlay in overlays:
        _overlay_id = str(_overlay["id"])
        _overlay_settings[_overlay_id] = _overlay
        _public_overlays.append(
            {
                "id": _overlay_id,
                "name": _overlay["name"],
                "visible": _overlay["visible"],
            }
        )

    return _public_overlays, _overlay_settings


@app.route("/get_telemetry_archive")
def flask_get_telemetry_archive():
    return json.dumps(current_payloads)


@app.route("/get_config")
def flask_get_config():
    return json.dumps(chasemapper_config)


@app.route("/get_aprsis_state")
def flask_get_aprsis_state():
    return json.dumps(_aprsis_state())


@app.route("/get_bearings")
def flask_get_bearings():
    return json.dumps(bearing_store.bearings)


@app.route("/get_car_track")
def flask_get_car_track():
    """Return today's chase-car path so refreshing the page restores the trail."""
    return json.dumps(car_track_cache.get_points())


# Some features of the web interface require comparisons with server time,
# so provide a route to grab it.
@app.route("/server_time")
def flask_get_server_time():
    return json.dumps(time.time())


@app.route("/tiles/<path:filename>")
def flask_server_tiles(filename):
    """ Serve up a file from the tile server location """
    global map_settings
    if map_settings["tile_server_enabled"]:
        return flask.send_from_directory(map_settings["tile_server_path"], filename)
    else:
        flask.abort(404)


@app.route("/overlays/kml/<overlay_id>")
def flask_server_kml_overlay(overlay_id):
    """ Serve up a configured KML overlay file. """
    global kml_overlay_settings

    _overlay = kml_overlay_settings.get(str(overlay_id))
    if _overlay is None:
        flask.abort(404)

    _overlay_path = _overlay["path"]
    if not os.path.isfile(_overlay_path):
        logging.error("Configured KML overlay does not exist: %s" % _overlay_path)
        flask.abort(404)

    return flask.send_from_directory(
        os.path.dirname(_overlay_path),
        os.path.basename(_overlay_path),
        mimetype="application/vnd.google-earth.kml+xml",
    )


# Recovery overlays (FAA airspace, TFRs, MD parcels). Auth gated when
# RECOVERY_API_KEY is set in the environment, intended for use behind
# Cloudflare with a Transform Rule injecting X-Recovery-Key.
RECOVERY_API_KEY = os.environ.get("RECOVERY_API_KEY", "")


def require_recovery_auth(view):
    """Gate a route behind X-Recovery-Key. A decorator rather than an in-body
    check so a new route cannot ship unauthenticated by forgetting the guard."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if RECOVERY_API_KEY:
            supplied = flask.request.headers.get("X-Recovery-Key", "")
            if not hmac.compare_digest(supplied, RECOVERY_API_KEY):
                return flask.jsonify({"error": "forbidden"}), 403
        return view(*args, **kwargs)

    return wrapper


def require_known_profile(view):
    """404 unless the route's profile_id names a configured telemetry profile."""
    @functools.wraps(view)
    def wrapper(profile_id, *args, **kwargs):
        if profile_id not in chasemapper_config.get("profiles", {}):
            return flask.jsonify({"error": "unknown profile"}), 404
        return view(profile_id, *args, **kwargs)

    return wrapper


@app.route("/airspace/status")
@require_recovery_auth
def flask_airspace_status():
    return flask.jsonify(airspace_cache.get_status())


@app.route("/airspace/refresh", methods=["POST"])
@require_recovery_auth
def flask_airspace_refresh():
    result = airspace_cache.force_refresh_all()
    return flask.jsonify(result)


@app.route("/airspace/<layer>")
@require_recovery_auth
def flask_airspace_layer(layer):
    data = airspace_cache.get_layer_geojson(layer)
    if data is None:
        return flask.jsonify({"error": "unknown or uncached layer", "layer": layer}), 404
    response = flask.jsonify(data)
    max_age = 60 if layer == "tfr" else 300
    response.headers["Cache-Control"] = "public, max-age=" + str(max_age)
    return response


@app.route("/parcels")
@require_recovery_auth
def flask_parcels():
    try:
        lat = float(flask.request.args.get("lat"))
        lon = float(flask.request.args.get("lon"))
        radius = float(flask.request.args.get("radius", 0.5))
        if not all(math.isfinite(v) for v in (lat, lon, radius)):
            raise ValueError("non-finite value")
    except (TypeError, ValueError):
        return flask.jsonify({"error": "lat, lon, radius are required numeric query params"}), 400
    if radius > parcel_proxy.RADIUS_MAX_MI:
        return flask.jsonify({
            "error": "radius capped at %s mi" % parcel_proxy.RADIUS_MAX_MI
        }), 400
    return flask.jsonify(parcel_proxy.get_parcels_near(lat, lon, radius))


# Per-profile geofence routes (KML upload + on-map drawing).
# See geofence.py for the storage, trash, and TTL semantics.


def _persist_geofence():
    """Write the store to disk and re-attach it to the live config."""
    geofence_mod.save_store(geofence_store_path, geofence_store)
    geofence_mod.attach_to_profiles(chasemapper_config, geofence_store)


def _broadcast_geofence(profile_id):
    """Push the current geofence (and trash availability) for a profile
    out to every connected client."""
    flask_emit_event(
        "geofence_update",
        {
            "profile": profile_id,
            "geofence": geofence_store.get("profiles", {}).get(profile_id),
            "has_trash": geofence_mod.has_trash(geofence_store, profile_id),
        },
    )


@app.route("/geofence/<profile_id>", methods=["GET"])
@require_recovery_auth
@require_known_profile
def flask_get_geofence(profile_id):
    return flask.jsonify({
        "geofence": geofence_store.get("profiles", {}).get(profile_id),
        "has_trash": geofence_mod.has_trash(geofence_store, profile_id),
    })


@app.route("/geofence/<profile_id>", methods=["POST"])
@require_recovery_auth
@require_known_profile
def flask_upload_geofence(profile_id):
    """Upload a geofence for the given profile.

    Accepts either:
      - a HAB Bounder KML, via multipart 'kml' field or raw body
        (Content-Type application/vnd.google-earth.kml+xml or text/xml), or
      - a JSON body with shape:
          {"polygon": [[lat,lon], ...],
           "min_alt": float, "max_alt": float, "remain": "inside"|"outside"}

    Either way, any previous geofence on this profile is moved to
    trash, so the user can hit Restore to undo the change.
    """
    global geofence_store

    # Decide which body shape we got. JSON when explicitly typed as
    # such, or when there's no multipart KML and the body looks like
    # JSON. Otherwise treat as KML bytes.
    geofence = None
    is_json = flask.request.is_json or (
        flask.request.mimetype == "application/json"
    )
    if is_json:
        payload = flask.request.get_json(silent=True) or {}
        try:
            geofence = geofence_mod.build_geofence_from_polygon(
                payload.get("polygon"),
                payload.get("min_alt"),
                payload.get("max_alt"),
                payload.get("remain"),
            )
        except geofence_mod.GeofenceParseError as e:
            return flask.jsonify({"error": str(e)}), 400
    else:
        kml_bytes = b""
        if "kml" in flask.request.files:
            kml_bytes = flask.request.files["kml"].read()
        elif flask.request.data:
            kml_bytes = flask.request.data

        if not kml_bytes:
            return flask.jsonify({"error": "no payload (multipart 'kml', raw KML, or JSON polygon)"}), 400
        if len(kml_bytes) > geofence_mod.MAX_KML_BYTES:
            return flask.jsonify({"error": "KML exceeds 5 MB limit"}), 413
        try:
            geofence = geofence_mod.parse_kml_geofence(kml_bytes)
        except geofence_mod.GeofenceParseError as e:
            logging.debug("Geofence parse error: %s", e)
            return flask.jsonify({"error": "KML could not be parsed as a valid geofence."}), 400

    geofence_mod.set_profile_geofence(geofence_store, profile_id, geofence)
    _persist_geofence()
    _broadcast_geofence(profile_id)
    logging.info(
        "Geofence set for profile '%s' (%d vertices, remain %s, alt %s..%s m, src=%s)"
        % (
            profile_id,
            len(geofence["polygon"]),
            geofence["remain"],
            geofence["min_alt"],
            geofence["max_alt"],
            "json" if is_json else "kml",
        )
    )
    return flask.jsonify({
        "ok": True,
        "geofence": geofence,
        "has_trash": geofence_mod.has_trash(geofence_store, profile_id),
    })


@app.route("/geofence/<profile_id>", methods=["DELETE"])
@require_recovery_auth
@require_known_profile
def flask_clear_geofence(profile_id):
    global geofence_store

    cleared = geofence_mod.clear_profile_geofence(geofence_store, profile_id)
    if cleared:
        _persist_geofence()
        logging.info("Geofence cleared (soft-deleted) for profile '%s'" % profile_id)

    _broadcast_geofence(profile_id)
    return flask.jsonify({
        "ok": True,
        "has_trash": geofence_mod.has_trash(geofence_store, profile_id),
    })


@app.route("/geofence/<profile_id>/restore", methods=["POST"])
@require_recovery_auth
@require_known_profile
def flask_restore_geofence(profile_id):
    """Reinstate the most recently cleared/overwritten geofence for a
    profile. The currently-active geofence (if any) is itself moved to
    trash, so the restore is undoable."""
    global geofence_store

    restored = geofence_mod.restore_latest(geofence_store, profile_id)
    if not restored:
        return flask.jsonify({"error": "no recoverable geofence in trash."}), 404

    _persist_geofence()
    _broadcast_geofence(profile_id)
    logging.info(
        "Geofence restored for profile '%s' (%d vertices)"
        % (profile_id, len(restored["polygon"]))
    )
    return flask.jsonify({
        "ok": True,
        "geofence": restored,
        "has_trash": geofence_mod.has_trash(geofence_store, profile_id),
    })


def flask_emit_event(event_name="none", data={}):
    """ Emit a socketio event to any clients. """
    socketio.emit(event_name, data, namespace="/chasemapper")


@socketio.on("connect", namespace="/chasemapper")
def on_client_connect():
    """ Push current APRS-IS state to any newly connected client. """
    from flask_socketio import emit
    emit("aprsis_state", _aprsis_state())


def sync_bearing_store_time_seq():
    """Keep the bearing handler aligned with server-authoritative time-sequence settings."""
    global bearing_store, chasemapper_config

    if bearing_store is None:
        return

    bearing_store.update_time_sequence(
        enabled=chasemapper_config["time_seq_enabled"],
        times=chasemapper_config["time_seq_times"],
        active=chasemapper_config["time_seq_active"],
        cycle=chasemapper_config["time_seq_cycle"],
    )


def sync_bearing_store_confidence_threshold():
    """Keep the bearing handler aligned with server-authoritative confidence settings."""
    global bearing_store, chasemapper_config

    if bearing_store is None:
        return

    bearing_store.update_confidence_threshold(
        chasemapper_config["doa_confidence_threshold"]
    )


@socketio.on("client_settings_update", namespace="/chasemapper")
def client_settings_update(data):
    global chasemapper_config, online_uploader, predictor

    _predictor_change = "none"
    if (chasemapper_config["pred_enabled"] == False) and (data["pred_enabled"] == True):
        _predictor_change = "restart"
    elif (chasemapper_config["pred_enabled"] == True) and (
        data["pred_enabled"] == False
    ):
        _predictor_change = "stop"

    _habitat_change = "none"
    if (chasemapper_config["habitat_upload_enabled"] == False) and (
        data["habitat_upload_enabled"] == True
    ):
        _habitat_change = "start"
    elif (chasemapper_config["habitat_upload_enabled"] == True) and (
        data["habitat_upload_enabled"] == False
    ):
        _habitat_change = "stop"

    _time_seq_state = {
        "time_seq_enabled": chasemapper_config["time_seq_enabled"],
        "time_seq_times": list(chasemapper_config["time_seq_times"]),
        "time_seq_active": chasemapper_config["time_seq_active"],
        "time_seq_cycle": chasemapper_config["time_seq_cycle"],
    }

    # Overwrite local config data with data from the client.
    chasemapper_config = data
    chasemapper_config.update(_time_seq_state)
    chasemapper_config.setdefault(
        "doa_confidence_threshold", default_config["doa_confidence_threshold"]
    )

    sync_bearing_store_time_seq()
    sync_bearing_store_confidence_threshold()

    if _predictor_change == "restart":
        # Wait until any current predictions have finished.
        while predictor_semaphore:
            time.sleep(0.1)
        # Attempt to start the predictor.
        initPredictor()
    elif _predictor_change == "stop":
        # Wait until any current predictions have finished.
        while predictor_semaphore:
            time.sleep(0.1)

        predictor = None

    # Start or Stop the Habitat Chase-Car Uploader.
    if _habitat_change == "start":
        if online_uploader == None:
            _tracker = chasemapper_config["profiles"][
                chasemapper_config["selected_profile"]
            ]["online_tracker"]
            if _tracker == "habitat":
                logging.error(
                    "Habitat uploader now deprecated due to Habitat retirement, not starting uploader."
                )
            elif _tracker == "sondehub":
                online_uploader = SondehubChaseUploader(
                    update_rate=chasemapper_config["habitat_update_rate"],
                    callsign=chasemapper_config["habitat_call"],
                )
            elif _tracker == "sondehubamateur":
                online_uploader = SondehubChaseUploader(
                    update_rate=chasemapper_config["habitat_update_rate"],
                    callsign=chasemapper_config["habitat_call"],
                    amateur=True
                )
            else:
                logging.error(
                    "Unknown Online Tracker %s, not starting uploader." % _tracker
                )

    elif _habitat_change == "stop":
        if online_uploader != None:
            online_uploader.close()
        online_uploader = None

    # Update the habitat uploader with a new update rate, if one has changed.
    if online_uploader != None:
        online_uploader.set_update_rate(chasemapper_config["habitat_update_rate"])
        online_uploader.set_callsign(chasemapper_config["habitat_call"])

    # Push settings back out to all clients.
    flask_emit_event("server_settings_update", chasemapper_config)


@socketio.on("time_seq_update", namespace="/chasemapper")
def time_seq_update(data):
    global chasemapper_config

    if "active" in data:
        try:
            _active = float(data["active"])
            if _active > 0:
                chasemapper_config["time_seq_active"] = _active
        except (TypeError, ValueError):
            pass

    if "cycle" in data:
        try:
            _cycle = float(data["cycle"])
            if _cycle > 0:
                chasemapper_config["time_seq_cycle"] = _cycle
        except (TypeError, ValueError):
            pass

    if chasemapper_config["time_seq_cycle"] < chasemapper_config["time_seq_active"]:
        chasemapper_config["time_seq_cycle"] = chasemapper_config["time_seq_active"]

    if "enabled" in data:
        chasemapper_config["time_seq_enabled"] = bool(data["enabled"])

    if "times" in data:
        try:
            _times = [float(_time) for _time in data["times"]]
        except (TypeError, ValueError):
            _times = None

        if _times is not None and len(_times) == len(chasemapper_config["time_seq_times"]):
            chasemapper_config["time_seq_times"] = _times
            chasemapper_config["time_seq_enabled"] = True

    if "slot" in data:
        try:
            _slot = int(data["slot"])
        except (TypeError, ValueError):
            _slot = None

        if _slot == -1:
            chasemapper_config["time_seq_enabled"] = False
            chasemapper_config["time_seq_times"] = [0, 0, 0, 0]
        elif _slot is not None and 0 <= _slot < len(chasemapper_config["time_seq_times"]):
            sync_bearing_store_time_seq()
            if bearing_store.get_current_seq_number(now=time.time()) < 0:
                _time_seq_times = list(chasemapper_config["time_seq_times"])
                _time_seq_times[_slot] = time.time()
                chasemapper_config["time_seq_times"] = _time_seq_times
                chasemapper_config["time_seq_enabled"] = True

    sync_bearing_store_time_seq()
    flask_emit_event("server_settings_update", chasemapper_config)


def handle_new_payload_position(data, log_position=True):

    _lat = data["lat"]
    _lon = data["lon"]
    _alt = data["alt"]
    _time_dt = data["time_dt"]
    _callsign = data["callsign"]

    _short_time = _time_dt.strftime("%H:%M:%S")

    # Multiple receivers on the same network will each broadcast their own copy of
    # a decoded packet, so the same telemetry (or an older, delayed frame) can arrive
    # more than once. Discard anything that isn't newer than the last track point,
    # otherwise the zero/negative time-step corrupts the ascent rate and turn rate.
    if _callsign in current_payload_tracks:
        _track_history = current_payload_tracks[_callsign].track_history
        if len(_track_history) > 0 and _time_dt <= _track_history[-1][0]:
            logging.debug(
                "Discarding duplicate/out-of-order telemetry for %s (packet time %s, last track point %s)."
                % (_callsign, _time_dt.isoformat(), _track_history[-1][0].isoformat())
            )
            return

    if _callsign not in current_payloads:
        # New callsign! Create entries in data stores.
        current_payload_tracks[_callsign] = GenericTrack(ascent_averaging=chasemapper_config["ascent_rate_averaging"])

        current_payloads[_callsign] = {
            "telem": {
                "callsign": _callsign,
                "position": [_lat, _lon, _alt],
                "max_alt": 0.0,
                "vel_v": 0.0,
                "speed": 0.0,
                "short_time": _short_time,
                "time_to_landing": "",
                "server_time": time.time(),
            },
            "path": [],
            "pred_path": [],
            "pred_landing": [],
            "burst": [],
            "abort_path": [],
            "abort_landing": [],
            "max_alt": 0.0,
            "snr": -255.0,
        }

    # Add new data into the payload's track, and get the latest ascent rate.
    current_payload_tracks[_callsign].add_telemetry(
        {"time": _time_dt, "lat": _lat, "lon": _lon, "alt": _alt, "comment": _callsign}
    )
    _state = current_payload_tracks[_callsign].get_latest_state()
    if _state != None:
        _vel_v = _state["ascent_rate"]
        _speed = _state["speed"]
        # If this payload is in descent, calculate the time to landing.
        # Use < -1.0, to avoid jitter when the payload is on the ground.
        if _vel_v < -1.0:
            # Try and get the altitude of the chase car - we use this as the expected 'ground' level.
            _car_state = car_track.get_latest_state()
            if _car_state != None:
                _ground_asl = _car_state["alt"]
            else:
                _ground_asl = 0.0

            # Calculate
            _ttl = time_to_landing(_alt, _vel_v, ground_asl=_ground_asl)
            if _ttl is None:
                _ttl = ""
            elif _ttl == 0:
                _ttl = "LANDED"
            else:
                _min = _ttl // 60
                _sec = _ttl % 60
                _ttl = "%02d:%02d" % (_min, _sec)
        else:
            _ttl = ""

    else:
        _vel_v = 0.0
        _speed = 0.0
        _ttl = ""

    # Now update the main telemetry store.
    current_payloads[_callsign]["telem"] = {
        "callsign": _callsign,
        "position": [_lat, _lon, _alt],
        "vel_v": _vel_v,
        "speed": _speed,
        "short_time": _short_time,
        "time_to_landing": _ttl,
        "server_time": time.time(),
    }

    current_payloads[_callsign]["path"].append([_lat, _lon, _alt])

    # Copy out any extra fields we may want to pass onto the GUI.
    for _field in EXTRA_FIELDS:
        if _field in data:
            current_payloads[_callsign]["telem"][_field] = data[_field]

    # Check if the current payload altitude is higher than our previous maximum altitude.
    if _alt > current_payloads[_callsign]["max_alt"]:
        current_payloads[_callsign]["max_alt"] = _alt

    # Add the payload maximum altitude into the telemetry snapshot dictionary.
    current_payloads[_callsign]["telem"]["max_alt"] = current_payloads[_callsign][
        "max_alt"
    ]

    # Update the web client.
    flask_emit_event("telemetry_event", current_payloads[_callsign]["telem"])

    # Add the position into the logger
    if chase_logger and log_position:
        chase_logger.add_balloon_telemetry(data)
    else:
        logging.debug("Point not logged.")


def handle_modem_stats(data):
    """ Basic handling of modem statistics data. If it matches a known payload, send the info to the client. """

    if data["source"] in current_payloads:
        flask_emit_event(
            "modem_stats_event", {"callsign": data["source"], "snr": data["snr"]}
        )


#
#   Predictor Code
#
predictor = None
predictor_semaphore = False
# End time of the current offline GFS dataset, used to detect when it goes stale mid-session.
predictor_model_end = None

predictor_thread_running = True
predictor_thread = None


def _clear_launch_preview(profile_name, reason):
    """Clear the temporary launch preview on every connected map."""
    global launch_preview_active_profile

    if launch_preview_active_profile == profile_name:
        launch_preview_active_profile = None
    flask_emit_event(
        "launch_preview_clear",
        {"profile": profile_name, "reason": reason},
    )


def _lock_launch_preview_for_live_prediction(callsign):
    """Hand a profile from its temporary preview to real balloon tracking."""
    profile_name = chasemapper_config.get("selected_profile", "")
    profile = chasemapper_config.get("profiles", {}).get(profile_name, {})
    balloon_callsigns = {
        cs.upper() for cs in profile.get("aprsis_balloon_callsigns", [])
    }
    if callsign.upper() not in balloon_callsigns:
        return
    if profile_name in launch_preview_locked_profiles:
        return

    launch_preview_locked_profiles.add(profile_name)
    _clear_launch_preview(profile_name, "live_prediction")
    flask_emit_event("aprsis_state", _aprsis_state())


# Float-profile rules, shared by the scheduled predictor and the one-shot
# launch preview so the two can't drift apart.


def _float_config():
    """(enabled, altitude, duration_hours) for CUSF float_profile / GHOUL flights."""
    return (
        bool(chasemapper_config.get("float_enabled", False)),
        float(chasemapper_config.get("float_altitude", 25000.0)),
        float(chasemapper_config.get("float_duration_hours", 24.0)),
    )


def _float_ceiling(float_altitude, current_altitude):
    """Tawhiri rejects a float altitude at or below the launch altitude."""
    return max(float_altitude, current_altitude + 1.0)


def _offline_float_burst(float_altitude, current_altitude):
    """The offline cusf_predictor has no float profile, so the float ceiling
    becomes a soft burst: the path ascends to it and then descends. Wrong
    after the float point, but it at least shows the ceiling."""
    return max(float_altitude, current_altitude + 100.0)


def _calculate_launch_preview(car_state, ascent_rate):
    """Run one prediction without adding a synthetic payload track."""
    launch_time = datetime.now(UTC)
    launch_altitude = float(car_state["alt"])
    burst_altitude = max(
        float(chasemapper_config["pred_burst"]), launch_altitude + 100.0
    )
    descent_rate = float(chasemapper_config["pred_desc_rate"])
    float_enabled, float_altitude, float_duration = _float_config()

    if predictor == "Tawhiri":
        if float_enabled:
            effective_float_altitude = _float_ceiling(float_altitude, launch_altitude)
            prediction = get_tawhiri_prediction(
                launch_datetime=launch_time,
                launch_latitude=car_state["lat"],
                launch_longitude=car_state["lon"],
                launch_altitude=launch_altitude,
                ascent_rate=ascent_rate,
                profile="float_profile",
                float_altitude=effective_float_altitude,
                stop_datetime=launch_time + timedelta(hours=float_duration),
            )
        else:
            prediction = get_tawhiri_prediction(
                launch_datetime=launch_time,
                launch_latitude=car_state["lat"],
                launch_longitude=car_state["lon"],
                launch_altitude=launch_altitude,
                burst_altitude=burst_altitude,
                ascent_rate=ascent_rate,
                descent_rate=descent_rate,
            )
        prediction_path = prediction["path"] if prediction else []
    else:
        offline_burst = (
            _offline_float_burst(float_altitude, launch_altitude)
            if float_enabled
            else burst_altitude
        )
        prediction_path = predictor.predict(
            launch_lat=car_state["lat"],
            launch_lon=car_state["lon"],
            launch_alt=launch_altitude,
            ascent_rate=ascent_rate,
            descent_rate=descent_rate,
            burst_alt=offline_burst,
            launch_time=launch_time,
            descent_mode=False,
        )

    if len(prediction_path) <= 1:
        return None

    prediction_path.insert(
        0,
        [0, car_state["lat"], car_state["lon"], launch_altitude],
    )
    output_path = [
        [point[1], point[2], point[3]] for point in prediction_path
    ]
    burst = max(output_path, key=lambda point: point[2])
    return {
        "pred_path": output_path,
        "pred_landing": output_path[-1],
        "burst": burst,
    }


@socketio.on("launch_preview_request", namespace="/chasemapper")
def launch_preview_request(data):
    """Run a one-shot launch prediction from the active APRS car fix."""
    global predictor_semaphore, launch_preview_active_profile
    from flask_socketio import emit

    profile_name = str(data.get("profile", "")).strip()
    active_profile_name = chasemapper_config.get("selected_profile", "")
    profile = _active_profile()

    try:
        ascent_rate = float(data.get("ascent_rate"))
    except (TypeError, ValueError):
        ascent_rate = 0.0

    def status(state, message):
        emit("launch_preview_status", {"state": state, "message": message})

    if profile_name != active_profile_name or profile is None:
        status("error", "The active telemetry profile changed; try again.")
        return
    if profile.get("car_source_type") != "aprsis":
        status("error", "Launch preview requires APRS-IS as the car source.")
        return
    if not profile.get("aprsis_active_car_callsign"):
        status("error", "No active APRS car callsign is configured.")
        return
    source_callsign = profile["aprsis_active_car_callsign"]
    if profile_name in launch_preview_locked_profiles:
        status("locked", "A live balloon prediction is active for this profile.")
        return
    if ascent_rate <= 0.0 or not math.isfinite(ascent_rate):
        status("error", "Ascent rate must be a positive number.")
        return
    car_state = (
        aprsis_listener.latest_car_position
        if aprsis_listener is not None
        else None
    )
    if car_state is None:
        status("error", "No APRS car position has been received yet.")
        return
    if not chasemapper_config.get("pred_enabled") or predictor is None:
        status("error", "The predictor is not ready or is disabled.")
        return
    if predictor_semaphore:
        status("error", "The predictor is busy; try again in a moment.")
        return

    predictor_semaphore = True
    status("running", "Running launch preview…")
    try:
        result = _calculate_launch_preview(car_state, ascent_rate)
        if result is None:
            status("error", "Launch preview failed; check the predictor model.")
            return
        if chasemapper_config.get("selected_profile", "") != profile_name:
            status("error", "The active telemetry profile changed; preview discarded.")
            return
        if profile_name in launch_preview_locked_profiles:
            status("locked", "A live balloon prediction is active for this profile.")
            return

        source_time = car_state["time"]
        if hasattr(source_time, "isoformat"):
            source_time = source_time.isoformat()
        result.update(
            {
                "profile": profile_name,
                "source_callsign": source_callsign,
                "source_time": str(source_time),
                "ascent_rate": ascent_rate,
            }
        )
        launch_preview_active_profile = profile_name
        flask_emit_event("launch_preview_update", result)
        status("ready", "Launch preview updated from the latest APRS car fix.")
        logging.info(
            "Launch preview updated for profile %s from %s at %.2f m/s.",
            profile_name,
            source_callsign,
            ascent_rate,
        )
    except Exception as error:
        logging.exception("Launch preview failed: %s", error)
        status("error", "Launch preview failed; check the server log.")
    finally:
        predictor_semaphore = False


def predictorThread():
    """ Run the predictor on a regular interval """
    global predictor_thread_running, chasemapper_config
    logging.info("Predictor loop started.")

    while predictor_thread_running:
        run_prediction()
        for i in range(int(chasemapper_config["pred_update_rate"])):
            time.sleep(1)
            if predictor_thread_running == False:
                break

    logging.info("Closed predictor loop.")


def fallback_to_tawhiri(reason):
    """ Fall back to online (Tawhiri) predictions, e.g. if GFS data is missing, stale, or failed to download. """
    global predictor, predictor_thread, predictor_model_end, chasemapper_config

    logging.warning("Falling back to online (Tawhiri) predictions - %s." % reason)
    predictor = "Tawhiri"
    predictor_model_end = None
    chasemapper_config["offline_predictions"] = False
    chasemapper_config["pred_model"] = "Tawhiri (Online - %s)" % reason
    flask_emit_event(
        "predictor_model_update", {"model": chasemapper_config["pred_model"]}
    )

    # Start up the predictor thread if it is not running.
    if predictor_thread is None:
        predictor_thread = Thread(target=predictorThread)
        predictor_thread.start()


def run_prediction():
    """ Run a Flight Path prediction """
    global chasemapper_config, current_payloads, current_payload_tracks, predictor, predictor_semaphore, predictor_model_end

    if chasemapper_config["pred_enabled"] == False:
        return

    if (chasemapper_config["offline_predictions"] == True) and (predictor == None):
        return

    # If the offline predictor is in use, check the GFS dataset still covers the
    # near future, and fall back to online predictions once it goes stale.
    if (
        (predictor is not None)
        and (predictor != "Tawhiri")
        and (predictor_model_end is not None)
    ):
        if (datetime.now(UTC) + timedelta(hours=4)) > predictor_model_end:
            fallback_to_tawhiri("GFS data expired")

    if predictor_semaphore:
        logging.debug("Skipping scheduled prediction because the predictor is busy.")
        return

    # Set the semaphore so we don't accidentally kill the predictor object while it's running.
    predictor_semaphore = True
    _payload_list = list(current_payload_tracks.keys())

    # SPOT trackers are display-only cross-reference traces. Their altitude
    # is noisy / often 0 and would produce nonsense descent-rate and landing
    # predictions, so skip them in the predictor loop.
    _spot_callsigns = {
        cs for cs, _env in chasemapper_config.get("spot_feeds", [])
    }

    for _payload in _payload_list:
        if _payload in _spot_callsigns:
            logging.debug("Skipping prediction for SPOT tracker %s." % _payload)
            continue

        # Check the age of the data.
        # No point re-running the predictor if the data is older than 30 seconds.
        _pos_age = current_payloads[_payload]["telem"]["server_time"]
        if (time.time() - _pos_age) > 30.0:
            logging.debug("Skipping prediction for %s due to old data." % _payload)
            continue

        _current_pos = current_payload_tracks[_payload].get_latest_state()
        _current_pos_list = [
            0,
            _current_pos["lat"],
            _current_pos["lon"],
            _current_pos["alt"],
        ]
        if current_payload_tracks[_payload].length() <= 1:
            logging.info(
                "Only %i point in this payload's track, skipping prediction.",
                current_payload_tracks[_payload].length(),
            )
            continue

        _pred_ok = False
        _abort_pred_ok = False

        if _current_pos["is_descending"]:
            _desc_rate = _current_pos["landing_rate"]
        else:
            _desc_rate = chasemapper_config["pred_desc_rate"]

        if _current_pos["alt"] > chasemapper_config["pred_burst"]:
            _burst_alt = _current_pos["alt"] + 100
        else:
            _burst_alt = chasemapper_config["pred_burst"]

        # Float (CUSF float_profile / GHOUL) applies only while ascending —
        # once the payload is coming down we revert to standard descent so
        # the landing point is still computed correctly.
        _float_enabled, _float_altitude, _float_duration = _float_config()
        _use_float = _float_enabled and not _current_pos["is_descending"]

        if predictor == "Tawhiri":
            logging.info("Requesting Prediction from Tawhiri for %s." % _payload)
            # Tawhiri requires that the burst altitude always be higher than the starting altitude.
            if _current_pos["is_descending"]:
                _burst_alt = _current_pos["alt"] + 1

            # Tawhiri requires that the ascent rate be > 0 for standard profiles.
            if _current_pos["ascent_rate"] < 0.1:
                _current_pos["ascent_rate"] = 0.1

            if _use_float:
                _eff_float_alt = _float_ceiling(_float_altitude, _current_pos["alt"])
                _stop_dt = _current_pos["time"] + timedelta(hours=_float_duration)
                logging.info(
                    "Float-profile prediction: float_alt=%.0f m, drift until %s"
                    % (_eff_float_alt, _stop_dt.isoformat())
                )
                _tawhiri = get_tawhiri_prediction(
                    launch_datetime=_current_pos["time"],
                    launch_latitude=_current_pos["lat"],
                    launch_longitude=_current_pos["lon"],
                    launch_altitude=_current_pos["alt"],
                    ascent_rate=_current_pos["ascent_rate"],
                    profile="float_profile",
                    float_altitude=_eff_float_alt,
                    stop_datetime=_stop_dt,
                )
            else:
                _tawhiri = get_tawhiri_prediction(
                    launch_datetime=_current_pos["time"],
                    launch_latitude=_current_pos["lat"],
                    launch_longitude=_current_pos["lon"],
                    launch_altitude=_current_pos["alt"],
                    burst_altitude=_burst_alt,
                    ascent_rate=_current_pos["ascent_rate"],
                    descent_rate=_desc_rate,
                )

            if _tawhiri:
                _pred_path = _tawhiri["path"]
                _dataset = _tawhiri["dataset"] + " (Online)"
                if _use_float:
                    _dataset += " [Float]"
                # Inform the client of the dataset age
                flask_emit_event("predictor_model_update", {"model": _dataset})

            else:
                _pred_path = []

        else:
            logging.info("Running Offline Predictor for %s." % _payload)
            if _use_float:
                logging.warning(
                    "Float mode enabled but offline predictor doesn't support "
                    "float_profile; falling back to burst at float altitude."
                )
                _offline_burst = _offline_float_burst(
                    _float_altitude, _current_pos["alt"]
                )
            else:
                _offline_burst = _burst_alt
            _pred_path = predictor.predict(
                launch_lat=_current_pos["lat"],
                launch_lon=_current_pos["lon"],
                launch_alt=_current_pos["alt"],
                ascent_rate=_current_pos["ascent_rate"],
                descent_rate=_desc_rate,
                burst_alt=_offline_burst,
                launch_time=_current_pos["time"],
                descent_mode=_current_pos["is_descending"],
            )

        if len(_pred_path) > 1:
            # Valid Prediction!
            _pred_path.insert(0, _current_pos_list)
            # Convert from predictor output format to a polyline.
            _pred_output = []
            for _point in _pred_path:
                _pred_output.append([_point[1], _point[2], _point[3]])

            current_payloads[_payload]["pred_path"] = _pred_output
            current_payloads[_payload]["pred_landing"] = _pred_output[-1]

            if _current_pos["is_descending"]:
                current_payloads[_payload]["burst"] = []
            else:
                # Determine the burst position.
                _cur_alt = 0.0
                _cur_idx = 0
                for i in range(len(_pred_output)):
                    if _pred_output[i][2] > _cur_alt:
                        _cur_alt = _pred_output[i][2]
                        _cur_idx = i

                current_payloads[_payload]["burst"] = _pred_output[_cur_idx]

            _pred_ok = True
            logging.info("Prediction Updated, %d data points." % len(_pred_path))
        else:
            current_payloads[_payload]["pred_path"] = []
            current_payloads[_payload]["pred_landing"] = []
            current_payloads[_payload]["burst"] = []
            logging.error("Prediction Failed, possible invalid or missing dataset.")
            flask_emit_event("predictor_model_update", {"model": "Dataset invalid."})

        # Abort predictions
        if (
            chasemapper_config["show_abort"]
            and (_current_pos["alt"] < chasemapper_config["pred_burst"])
            and (_current_pos["is_descending"] == False)
        ):

            if predictor == "Tawhiri":
                logging.info(
                    "Requesting Abort Prediction from Tawhiri for %s." % _payload
                )

                # Tawhiri requires that the ascent rate be > 0 for standard profiles.
                if _current_pos["ascent_rate"] < 0.1:
                    _current_pos["ascent_rate"] = 0.1

                _tawhiri = get_tawhiri_prediction(
                    launch_datetime=_current_pos["time"],
                    launch_latitude=_current_pos["lat"],
                    launch_longitude=_current_pos["lon"],
                    launch_altitude=_current_pos["alt"],
                    burst_altitude=_current_pos["alt"] + 200,
                    ascent_rate=_current_pos["ascent_rate"],
                    descent_rate=_desc_rate,
                )

                if _tawhiri:
                    _abort_pred_path = _tawhiri["path"]

                else:
                    _abort_pred_path = []

            else:
                logging.info("Running Offline Abort Predictor for: %s." % _payload)

                _abort_pred_path = predictor.predict(
                    launch_lat=_current_pos["lat"],
                    launch_lon=_current_pos["lon"],
                    launch_alt=_current_pos["alt"],
                    ascent_rate=_current_pos["ascent_rate"],
                    descent_rate=_desc_rate,
                    burst_alt=_current_pos["alt"] + 200,
                    launch_time=_current_pos["time"],
                    descent_mode=_current_pos["is_descending"],
                )

            if len(_abort_pred_path) > 1:
                # Valid Prediction!
                _abort_pred_path.insert(0, _current_pos_list)
                # Convert from predictor output format to a polyline.
                _abort_pred_output = []
                for _point in _abort_pred_path:
                    _abort_pred_output.append([_point[1], _point[2], _point[3]])

                current_payloads[_payload]["abort_path"] = _abort_pred_output
                current_payloads[_payload]["abort_landing"] = _abort_pred_output[-1]

                _abort_pred_ok = True
                logging.info(
                    "Abort Prediction Updated, %d data points." % len(_abort_pred_path)
                )
            else:
                current_payloads[_payload]["abort_path"] = []
                current_payloads[_payload]["abort_landing"] = []
                logging.error("Prediction Failed, possible invalid or missing dataset.")
                flask_emit_event("predictor_model_update", {"model": "Dataset invalid."})
        else:
            # Zero the abort path and landing
            current_payloads[_payload]["abort_path"] = []
            current_payloads[_payload]["abort_landing"] = []

        # Send the web client the updated prediction data.
        if _pred_ok or _abort_pred_ok:
            _client_data = {
                "callsign": _payload,
                "pred_path": current_payloads[_payload]["pred_path"],
                "pred_landing": current_payloads[_payload]["pred_landing"],
                "burst": current_payloads[_payload]["burst"],
                "abort_path": current_payloads[_payload]["abort_path"],
                "abort_landing": current_payloads[_payload]["abort_landing"],
            }
            flask_emit_event("predictor_update", _client_data)
            _lock_launch_preview_for_live_prediction(_payload)

            # Add the prediction run to the logger.
            if chase_logger:
                chase_logger.add_balloon_prediction(_client_data)

    # Clear the predictor-running semaphore
    predictor_semaphore = False


def initPredictor():
    global predictor, predictor_thread, predictor_model_end, chasemapper_config, pred_settings

    if chasemapper_config["offline_predictions"]:
        # Attempt to initialize an Offline Predictor instance
        try:
            from cusfpredict.predict import Predictor
            from cusfpredict.utils import gfs_model_age, available_gfs

            # Check if we have any GFS data
            _model_age = gfs_model_age(pred_settings["gfs_path"])
            if _model_age == "Unknown":
                logging.error("No GFS data in directory.")
                fallback_to_tawhiri("No GFS data")
            else:
                # Check model contains data to at least 4 hours into the future.
                (_model_start, _model_end) = available_gfs(pred_settings["gfs_path"])
                _model_now = datetime.now(UTC) + timedelta(hours=4)
                if (_model_now < _model_start) or (_model_now > _model_end):
                    # No suitable GFS data!
                    logging.error("GFS Data in directory does not cover now!")
                    fallback_to_tawhiri("Old GFS data")

                else:
                    chasemapper_config["pred_model"] = _model_age + " (Offline)"
                    flask_emit_event(
                        "predictor_model_update", {"model": _model_age + " (Offline)"}
                    )
                    predictor = Predictor(
                        bin_path=pred_settings["pred_binary"],
                        gfs_path=pred_settings["gfs_path"],
                    )
                    predictor_model_end = _model_end

                    # Start up the predictor thread if it is not running.
                    if predictor_thread == None:
                        predictor_thread = Thread(target=predictorThread)
                        predictor_thread.start()

                    # Set the predictor to enabled, and update the clients.
                    chasemapper_config["offline_predictions"] = True

        except Exception as e:
            traceback.print_exc()
            logging.error("Loading predictor failed: " + str(e))
            print("Loading Predictor failed.")
            predictor = None
            fallback_to_tawhiri("Offline predictor failed to load")

    else:
        # No initialization required for the online predictor
        predictor = "Tawhiri"
        flask_emit_event("predictor_model_update", {"model": "Tawhiri"})

        # Start up the predictor thread if it is not running.
        if predictor_thread == None:
            predictor_thread = Thread(target=predictorThread)
            predictor_thread.start()

    flask_emit_event("server_settings_update", chasemapper_config)


def model_download_finished(result):
    """ Callback for when the model download is finished """
    global chasemapper_config, predictor
    if result == "OK":
        # Downloader reported OK, restart the predictor.
        chasemapper_config["offline_predictions"] = True
        initPredictor()
    else:
        # Downloader reported an error, pass on to the client.
        flask_emit_event("predictor_model_update", {"model": result})
        # If we don't have a working offline predictor with current data, fall back
        # to online predictions rather than leaving the predictor disabled.
        if (predictor is None) or (predictor == "Tawhiri"):
            fallback_to_tawhiri("Model download failed")


@socketio.on("download_model", namespace="/chasemapper")
def download_new_model(data):
    """ Trigger a download of a new weather model """
    global pred_settings, model_download_running
    # Don't action anything if there is a model download already running

    logging.info("Web Client Initiated request for new predictor data.")

    if pred_settings["pred_model_download"] == "none":
        logging.info("No GFS model download command specified.")
        flask_emit_event("predictor_model_update", {"model": "No model download cmd."})
        return
    else:
        _model_cmd = pred_settings["pred_model_download"]
        flask_emit_event("predictor_model_update", {"model": "Downloading Model."})

        _status = predictor_spawn_download(_model_cmd, model_download_finished)
        flask_emit_event("predictor_model_update", {"model": _status})


@app.route("/download_model")
def download_new_model_2():
    """ Trigger a download of a new weather model via a GET request """
    global pred_settings, model_download_running

    logging.info("Web Client Initiated request for new predictor data via /download_model.")

    if pred_settings["pred_model_download"] == "none":
        logging.info("No GFS model download command specified.")
        return "No model download cmd."
    else:
        _model_cmd = pred_settings["pred_model_download"]
        _status = predictor_spawn_download(_model_cmd, model_download_finished)
        return _status


# Data Clearing Functions
@socketio.on("payload_data_clear", namespace="/chasemapper")
def clear_payload_data(data):
    """ Clear the payload data store """
    global predictor_semaphore, current_payloads, current_payload_tracks
    global launch_preview_locked_profiles
    logging.warning("Client requested all payload data be cleared.")
    # Wait until any current predictions have finished running.
    while predictor_semaphore:
        time.sleep(0.1)

    current_payloads = {}
    current_payload_tracks = {}
    launch_preview_locked_profiles.clear()
    _clear_launch_preview(chasemapper_config.get("selected_profile", ""), "payload_clear")
    flask_emit_event("aprsis_state", _aprsis_state())


@socketio.on("car_data_clear", namespace="/chasemapper")
def clear_car_data(data):
    """ Clear out the car position track """
    global car_track
    logging.warning("Client requested all chase car data be cleared.")
    car_track = GenericTrack()
    # Re-apply configured gating thresholds, which the fresh GenericTrack
    # would otherwise reset to its defaults.
    car_track.heading_gate_threshold = chasemapper_config["car_speed_gate"]
    car_track.turn_rate_threshold = chasemapper_config["turn_rate_threshold"]
    car_track_cache.clear()


@socketio.on("bearing_store_clear", namespace="/chasemapper")
def clear_bearing_data(data):
    """ Clear all bearing data """
    global bearing_store
    logging.warning("Client requested bearing data be cleared.")
    bearing_store.flush()
    flask_emit_event("server_bearings_cleared", {"foo":"bar"})


@socketio.on("mark_recovered", namespace="/chasemapper")
def mark_payload_recovered(data):
    """ Mark a payload as recovered, by uploading a station position """
    global online_uploader

    print(data)

    _serial = data["payload_call"]
    _callsign = data["my_call"]
    _lat = data["last_pos"][0]
    _lon = data["last_pos"][1]
    _alt = data["last_pos"][2]
    _msg = data["message"]
    _recovered = data["recovered"]

    if online_uploader != None:
        online_uploader.mark_payload_recovered(
            serial = _serial,
            callsign = _callsign,
            lat = _lat, 
            lon = _lon, 
            alt = _alt, 
            message = _msg, 
            recovered=_recovered
            )
    else:
        logging.error("No Online Tracker enabled, could not mark payload as recovered.")


# Incoming telemetry handlers


def ozi_listener_callback(data):
    """ Handle a OziMux input message """
    # OziMux message contains:
    # {'lat': -34.87915, 'comment': 'Telemetry Data', 'alt': 26493.0, 'lon': 139.11883, 'time': datetime.datetime(2018, 7, 16, 10, 55, 49, tzinfo=tzutc())}
    output = {}
    output["lat"] = float(data["lat"])
    output["lon"] = float(data["lon"])
    output["alt"] = float(data["alt"])
    output["callsign"] = "Payload"
    output["time_dt"] = data["time"]

    logging.info(
        "OziMux Data: %.5f, %.5f, %.1f" % (data["lat"], data["lon"], data["alt"])
    )

    try:
        handle_new_payload_position(output)
    except Exception as e:
        logging.error("Error Handling Payload Position - %s" % str(e))


def udp_listener_summary_callback(data):
    """ Handle a Payload Summary Message from UDPListener """

    # Modem stats messages are also passed in via this callback.
    # handle them separately.
    if data["type"] == "MODEM_STATS":
        handle_modem_stats(data)
        return

    # Otherwise, we have a PAYLOAD_SUMMARY message.

    # Extract the fields we need.
    # Convert to something generic we can pass onwards.
    output = {}
    output["lat"] = float(data["latitude"])
    output["lon"] = float(data["longitude"])
    output["alt"] = float(data["altitude"])
    output["callsign"] = data["callsign"]

    if "time" in data.keys():
        _time = data["time"]
    else:
        _time = "??:??:??"

    logging.info(
        "Horus UDP Data: %s, %s, %.5f, %.5f, %.1f"
        % (output["callsign"], _time, output["lat"], output["lon"], output["alt"])
    )

    # Process the 'short time' value if we have been provided it.
    if "time" in data.keys():
        output["time_dt"] = fix_datetime(data["time"])
        # _full_time = datetime.utcnow().strftime("%Y-%m-%dT") + data['time'] + "Z"
        # output['time_dt'] = parse(_full_time)
    else:
        # Otherwise use the current UTC time.

        output["time_dt"] = datetime.now(timezone.utc)

    # Copy out any extra fields that we want to pass on to the GUI.
    for _field in EXTRA_FIELDS:
        if _field in data:
            output[_field] = data[_field]

    try:
        handle_new_payload_position(output)
    except Exception as e:
        logging.error("Error Handling Payload Position - %s" % str(e))


def udp_listener_car_callback(data):
    """ Handle car position data """
    # TODO: Make a generic car position function, and have this function pass data into it
    # so we can add support for other chase car position inputs.
    global car_track, online_uploader, bearing_store
    _lat = float(data["latitude"])
    _lon = float(data["longitude"])

    # Handle when GPSD and/or other GPS data sources return a n/a for altitude.
    try:
        _alt = float(data["altitude"])
    except:
        _alt = 0.0

    _comment = "CAR"
    _time_dt = datetime.now(timezone.utc)

    logging.debug("Car Position: %.5f, %.5f" % (_lat, _lon))

    _car_position_update = {
        "time": _time_dt,
        "lat": _lat,
        "lon": _lon,
        "alt": _alt,
        "comment": _comment,
    }
    # Add in true heading data if we have been supplied it (e.g. from a uBlox NEO-M8U device)
    if "heading" in data:
        _car_position_update["heading"] = data["heading"]

    if "heading_status" in data:
        _car_position_update["heading_status"] = data["heading_status"]
    

    car_track.add_telemetry(_car_position_update)

    # Persist the day's chase-car path so a page refresh restores the trail.
    try:
        car_track_cache.add_point(_lat, _lon, _alt, data.get("heading", 0.0))
    except Exception as e:
        logging.debug("car_track_cache append failed: %s", e)

    _state = car_track.get_latest_state()
    _heading = _state["heading"]
    _heading_status = _state["heading_status"]
    _heading_valid = _state["heading_valid"]
    _speed = _state["speed"]


    _car_telem = {
            "callsign": "CAR",
            "position": [_lat, _lon, _alt],
            "vel_v": 0.0,
            "heading": _heading,
            "heading_valid": _heading_valid,
            "heading_status": _heading_status,
            "speed": _speed,
    }

    if 'replay_time' in data:
        # We are getting data from a log file replay, make sure to pass this on
        _replay_time = parse(data['replay_time'])
        _replay_time_str = _replay_time.strftime("%Y-%m-%d %H:%M:%SZ")
        _car_telem['replay_time'] = _replay_time_str

    # Add in some additional status fields if we have them.
    if 'numSV' in data:
        _car_telem['numSV'] = data['numSV']

    # Push the new car position to the web client
    flask_emit_event(
        "telemetry_event",
        _car_telem
    )

    # Update the Online Position Uploader, if one exists.
    if online_uploader != None:
        online_uploader.update_position(data)

    # Update the bearing store with the current car state (position & bearing)
    if bearing_store != None:
        bearing_store.update_car_position(_state)

    # Add the car position to the logger, but only if we are moving (>10kph = ~3m/s)
    # .. or if are receving bearing data, in which case we want to store high resolution position data.
    if ( (_speed > 3.0) or bearing_mode) and chase_logger:
        _car_position_update["speed"] = _speed
        _car_position_update["heading"] = _heading
        chase_logger.add_car_position(_car_position_update)


def udp_listener_bearing_callback(data):
    global bearing_store, bearing_mode, chase_logger

    if bearing_store != None:
        _bearing_stored = bearing_store.add_bearing(data)
        if _bearing_stored:
            bearing_mode = True
        if _bearing_stored and chase_logger:
            chase_logger.add_bearing(data)



@socketio.on("add_manual_bearing", namespace="/chasemapper")
def add_manual_bearing(data):
    # Add a user-supplied bearing from the web interface
    udp_listener_bearing_callback(data)


# Data Age Monitoring Thread
data_monitor_thread_running = True


def check_data_age():
    """ Regularly check the age of the payload data, and clear if latest position is older than X minutes."""
    global current_payloads, chasemapper_config, predictor_semaphore

    while data_monitor_thread_running:
        _now = time.time()
        _callsigns = list(current_payloads.keys())

        for _call in _callsigns:
            try:
                _latest_time = current_payloads[_call]["telem"]["server_time"]
                if (_now - _latest_time) > (
                    chasemapper_config["payload_max_age"] * 60.0
                ):
                    # Data is older than our maximum age!
                    # Make sure we do not have a predictor cycle running.
                    while predictor_semaphore:
                        time.sleep(0.1)

                    # Remove this payload from our global data stores.
                    current_payloads.pop(_call)
                    current_payload_tracks.pop(_call)

                    logging.info(
                        "Payload %s telemetry older than maximum age - removed from data store."
                        % _call
                    )
            except Exception as e:
                logging.error("Error checking payload data age - %s" % str(e))

        time.sleep(2)


def start_listeners(profile):
    """ Stop any currently running listeners, and startup a set of data listeners based on the supplied profile 
    
    Args:
        profile (dict): A dictionary containing:
            'name' (str): Profile name
            'telemetry_source_type' (str): Data source type (ozimux or horus_udp)
            'telemetry_source_port' (int): Data source port
            'car_source_type' (str): Car Position source type (none, horus_udp, gpsd, or station)
            'car_source_port' (int): Car Position source port
            'online_tracker' (str): Which online tracker to upload chase-car info to ('sondehub' or 'sondehubamateur')
    """
    global data_listeners, current_profile, online_uploader, chasemapper_config, aprsis_listener

    current_profile = profile
    aprsis_listener = None

    # Stop any existing listeners.
    for _thread in data_listeners:
        try:
            _thread.close()
        except Exception as e:
            logging.error("Error closing thread - %s" % str(e))

    # Shut-down any online uploaders
    if online_uploader != None:
        online_uploader.close()
        online_uploader = None

    # Reset the listeners array.
    data_listeners = []

    # Start up a new online uploader immediately if uploading is already enabled.
    if chasemapper_config["habitat_upload_enabled"] == True:
        if profile["online_tracker"] == "habitat":
            logging.error(
                "Habitat uploader now deprecated due to Habitat retirement, not starting uploader."
            )
        elif profile["online_tracker"] == "sondehub":
            online_uploader = SondehubChaseUploader(
                update_rate=chasemapper_config["habitat_update_rate"],
                callsign=chasemapper_config["habitat_call"],
            )
        elif profile["online_tracker"] == "sondehubamateur":
            online_uploader = SondehubChaseUploader(
                update_rate=chasemapper_config["habitat_update_rate"],
                callsign=chasemapper_config["habitat_call"],
                amateur=True
            )
        else:
            logging.error(
                "Unknown Online Tracker %s, not starting uploader"
                % (profile["online_tracker"])
            )

    # Start up a OziMux listener, if we are using one.
    if profile["telemetry_source_type"] == "ozimux":
        logging.info(
            "Using OziMux data source on UDP Port %d" % profile["telemetry_source_port"]
        )
        _ozi_listener = OziListener(
            telemetry_callback=ozi_listener_callback,
            port=profile["telemetry_source_port"],
        )
        data_listeners.append(_ozi_listener)

    # Start up UDP Broadcast Listener (which we use for car positions even if not for the payload)

    # Case 1 - Both telemetry and car position sources are set to horus_udp, and have the same port set. Only start a single UDP listener
    if (
        (profile["telemetry_source_type"] == "horus_udp")
        and (profile["car_source_type"] == "horus_udp")
        and (profile["car_source_port"] == profile["telemetry_source_port"])
    ):
        # In this case, we start a single Horus UDP listener.
        logging.info(
            "Starting single Horus UDP listener on port %d"
            % profile["telemetry_source_port"]
        )
        _telem_horus_udp_listener = UDPListener(
            summary_callback=udp_listener_summary_callback,
            gps_callback=udp_listener_car_callback,
            bearing_callback=udp_listener_bearing_callback,
            port=profile["telemetry_source_port"],
        )
        _telem_horus_udp_listener.start()
        data_listeners.append(_telem_horus_udp_listener)

    else:
        if profile["telemetry_source_type"] == "horus_udp":
            # Telemetry via Horus UDP - Start up a listener
            logging.info(
                "Starting Telemetry Horus UDP listener on port %d"
                % profile["telemetry_source_port"]
            )
            _telem_horus_udp_listener = UDPListener(
                summary_callback=udp_listener_summary_callback,
                gps_callback=None,
                bearing_callback=udp_listener_bearing_callback,
                port=profile["telemetry_source_port"],
            )
            _telem_horus_udp_listener.start()
            data_listeners.append(_telem_horus_udp_listener)

        if profile["car_source_type"] == "horus_udp":
            # Car Position via Horus UDP - Start up a listener
            logging.info(
                "Starting Car Position Horus UDP listener on port %d"
                % profile["car_source_port"]
            )
            _car_horus_udp_listener = UDPListener(
                summary_callback=None,
                gps_callback=udp_listener_car_callback,
                bearing_callback=udp_listener_bearing_callback,
                port=profile["car_source_port"],
            )
            _car_horus_udp_listener.start()
            data_listeners.append(_car_horus_udp_listener)

        elif profile["car_source_type"] == "gpsd":
            # GPSD Car Position Source
            logging.info("Starting GPSD Car Position Listener.")
            _gpsd_gps = GPSDAdaptor(
                hostname=chasemapper_config["car_gpsd_host"],
                port=chasemapper_config["car_gpsd_port"],
                callback=udp_listener_car_callback,
            )
            data_listeners.append(_gpsd_gps)

        elif profile["car_source_type"] == "serial":
            # Serial GPS Source.
            logging.info("Starting Serial GPS Listener.")
            _serial_gps = SerialGPS(
                serial_port=chasemapper_config["car_serial_port"],
                serial_baud=chasemapper_config["car_serial_baud"],
                callback=udp_listener_car_callback,
            )
            data_listeners.append(_serial_gps)

        elif profile["car_source_type"] == "aprsis":
            logging.info(
                "Starting APRS-IS listener for profile '%s' (cars=%s, balloons=%s)."
                % (
                    profile.get("name", "?"),
                    profile.get("aprsis_car_callsigns", []),
                    profile.get("aprsis_balloon_callsigns", []),
                )
            )
            _aprsis = APRSISListener(
                server=chasemapper_config["aprsis_server"],
                port=chasemapper_config["aprsis_port"],
                login_callsign=chasemapper_config["aprsis_login_callsign"],
                balloon_callsigns=profile.get("aprsis_balloon_callsigns", []),
                car_callsigns=profile.get("aprsis_car_callsigns", []),
                active_car_callsign=profile.get("aprsis_active_car_callsign", ""),
                summary_callback=udp_listener_summary_callback,
                car_callback=udp_listener_car_callback,
            )
            _aprsis.start()
            data_listeners.append(_aprsis)
            aprsis_listener = _aprsis

        elif profile["car_source_type"] == "station":
            logging.info("Using Stationary receiver position.")

        else:
            # No Car position.
            logging.info("No car position data source.")

    # SPOT GPS tracker feeds — global, independent of profile selection.
    if chasemapper_config.get("spot_enabled") and chasemapper_config.get("spot_feeds"):
        _spot = SPOTListener(
            feeds=chasemapper_config["spot_feeds"],
            summary_callback=udp_listener_summary_callback,
            poll_interval=chasemapper_config.get("spot_poll_interval", 300),
        )
        _spot.start()
        data_listeners.append(_spot)


@socketio.on("profile_change", namespace="/chasemapper")
def profile_change(data):
    """ Client has requested a profile change """
    global chasemapper_config
    logging.info("Client requested change to profile: %s" % data)

    previous_profile = chasemapper_config.get("selected_profile", "")
    _clear_launch_preview(previous_profile, "profile_change")

    # Change the profile, and restart the listeners.
    chasemapper_config["selected_profile"] = data
    start_listeners(
        chasemapper_config["profiles"][chasemapper_config["selected_profile"]]
    )

    # Update all clients with the new profile selection
    flask_emit_event("server_settings_update", chasemapper_config)
    flask_emit_event("aprsis_state", _aprsis_state())


def _active_profile():
    """Return the dict for the currently selected profile, or None."""
    name = chasemapper_config.get("selected_profile", "")
    return chasemapper_config.get("profiles", {}).get(name)


def _aprsis_state():
    p = _active_profile() or {}
    return {
        "profile_name": p.get("name", ""),
        "active_car": p.get("aprsis_active_car_callsign", ""),
        "car_callsigns": p.get("aprsis_car_callsigns", []),
        "balloon_callsigns": p.get("aprsis_balloon_callsigns", []),
        "connected": (aprsis_listener is not None) and aprsis_listener.connected,
        "launch_preview_locked": (
            chasemapper_config.get("selected_profile", "")
            in launch_preview_locked_profiles
        ),
    }


@socketio.on("aprsis_set_car_callsign", namespace="/chasemapper")
def aprsis_set_car_callsign(data):
    global aprsis_listener
    cs = data.get("callsign", "").strip().upper()
    p = _active_profile()
    if not cs or p is None:
        return
    p["aprsis_active_car_callsign"] = cs
    if aprsis_listener is not None:
        aprsis_listener.set_active_car_callsign(cs)
    logging.info("APRS-IS active car callsign: %s (profile %s)" % (cs, p.get("name")))
    flask_emit_event("aprsis_state", _aprsis_state())
    # Also push the full settings so client-side config copies stay in sync;
    # otherwise a later client_settings_update would clobber this change.
    flask_emit_event("server_settings_update", chasemapper_config)


@socketio.on("aprsis_add_car_callsign", namespace="/chasemapper")
def aprsis_add_car_callsign(data):
    global aprsis_listener
    cs = data.get("callsign", "").strip().upper()
    p = _active_profile()
    if not cs or p is None:
        return
    if cs not in p.get("aprsis_car_callsigns", []):
        p.setdefault("aprsis_car_callsigns", []).append(cs)
    if aprsis_listener is not None:
        aprsis_listener.add_car_callsign(cs)
    flask_emit_event("aprsis_state", _aprsis_state())
    flask_emit_event("server_settings_update", chasemapper_config)


@socketio.on("aprsis_add_balloon_callsign", namespace="/chasemapper")
def aprsis_add_balloon_callsign(data):
    global aprsis_listener
    cs = data.get("callsign", "").strip().upper()
    p = _active_profile()
    if not cs or p is None:
        return
    if cs not in p.get("aprsis_balloon_callsigns", []):
        p.setdefault("aprsis_balloon_callsigns", []).append(cs)
    if aprsis_listener is not None:
        aprsis_listener.add_balloon_callsign(cs)
    flask_emit_event("aprsis_state", _aprsis_state())
    flask_emit_event("server_settings_update", chasemapper_config)


@socketio.on("aprsis_remove_callsign", namespace="/chasemapper")
def aprsis_remove_callsign(data):
    """Remove a callsign from the active profile's car or balloon list."""
    global aprsis_listener
    cs = data.get("callsign", "").strip().upper()
    kind = data.get("kind", "")  # "car" or "balloon"
    p = _active_profile()
    if not cs or p is None or kind not in ("car", "balloon"):
        return
    key = "aprsis_car_callsigns" if kind == "car" else "aprsis_balloon_callsigns"
    if cs in p.get(key, []):
        p[key].remove(cs)
    # If the active car was removed, fall back to the first remaining one (or empty)
    if kind == "car" and p.get("aprsis_active_car_callsign") == cs:
        new_active = p["aprsis_car_callsigns"][0] if p["aprsis_car_callsigns"] else ""
        p["aprsis_active_car_callsign"] = new_active
        if aprsis_listener is not None and new_active:
            aprsis_listener.set_active_car_callsign(new_active)
    flask_emit_event("aprsis_state", _aprsis_state())
    flask_emit_event("server_settings_update", chasemapper_config)


@socketio.on("device_position", namespace="/chasemapper")
def device_position_update(data):
    """ Accept a device position update from a client and process it as if it was a chase car position """
    try:
        udp_listener_car_callback(data)
    except:
        pass


class WebHandler(logging.Handler):
    """ Logging Handler for sending log messages via Socket.IO to a Web Client """

    def emit(self, record):
        """ Emit a log message via SocketIO """
        # record.getMessage() applies record.args to the format string — without it,
        # lazy log calls like logging.info("foo %s", x) reach the client unformatted.
        msg = record.getMessage()
        if msg and "socket.io" not in msg:
            log_data = {
                "level": record.levelname,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "msg": msg,
            }
            socketio.emit("log_event", log_data, namespace="/chasemapper")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="horusmapper.cfg",
        help="Configuration file.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=False, help="Verbose output."
    )
    parser.add_argument(
        "-l",
        "--log",
        type=str,
        default=None,
        help="Custom log file name. (Default: ./log_files/<timestamp>.log",
    )
    parser.add_argument(
        "--nolog", action="store_true", default=False, help="Inhibit all logging."
    )
    args = parser.parse_args()

    # Configure logging
    if args.verbose:
        _log_level = logging.DEBUG
    else:
        _log_level = logging.INFO

    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(message)s",
        stream=sys.stdout,
        level=_log_level,
    )
    # Make flask & socketio only output errors, not every damn GET request.
    logging.getLogger("requests").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("socketio").setLevel(logging.ERROR)
    logging.getLogger("engineio").setLevel(logging.ERROR)

    web_handler = WebHandler()
    logging.getLogger().addHandler(web_handler)

    # Start the Chase Logger (if logging not inhibited.)
    if not args.nolog:
        chase_logger = ChaseLogger(filename=args.log)
    else:
        logging.info("Chase Logging has been inhibited, not starting logger.")

    # Attempt to read in config file.
    chasemapper_config = read_config(args.config)
    # Die if we cannot read a valid config file.
    if chasemapper_config == None:
        logging.critical("Could not read configuration data. Exiting")
        sys.exit(1)

    # Add in Chasemapper version information.
    chasemapper_config["version"] = CHASEMAPPER_VERSION

    # Keep overlay filesystem paths server-side only.
    chasemapper_config["kml_overlays"], kml_overlay_settings = split_kml_overlay_settings(
        chasemapper_config["kml_overlays"]
    )

    # Load per-profile geofences (HAB Bounder KML uploads). The sidecar
    # file lives next to the active config so multi-config setups stay
    # isolated. Each profile dict gets a "geofence" key (None if none
    # uploaded) so the frontend always sees a consistent shape.
    geofence_store_path = os.path.join(
        os.path.dirname(os.path.abspath(args.config)) or ".",
        "geofences.json",
    )
    geofence_store = geofence_mod.load_store(geofence_store_path)
    # Drop trash entries older than the 2-day TTL on startup so
    # has_trash reflects only actually-recoverable geofences.
    if geofence_mod.prune_trash(geofence_store):
        geofence_mod.save_store(geofence_store_path, geofence_store)
    geofence_mod.attach_to_profiles(chasemapper_config, geofence_store)

    # Copy out the predictor settings to another dictionary.
    pred_settings = {
        "pred_binary": chasemapper_config["pred_binary"],
        "gfs_path": chasemapper_config["pred_gfs_directory"],
        "pred_model_download": chasemapper_config["pred_model_download"],
    }

    # Copy out Offline Map Settings
    map_settings = {
        "tile_server_enabled": chasemapper_config["tile_server_enabled"],
        "tile_server_path": chasemapper_config["tile_server_path"],
    }

    # Initialise Bearing store
    bearing_store = Bearings(
        socketio_instance=socketio,
        max_bearings=chasemapper_config["max_bearings"],
        max_bearing_age=chasemapper_config["max_bearing_age"],
        time_seq_enabled=chasemapper_config["time_seq_enabled"],
        time_seq_times=chasemapper_config["time_seq_times"],
        time_seq_active=chasemapper_config["time_seq_active"],
        time_seq_cycle=chasemapper_config["time_seq_cycle"],
        doa_confidence_threshold=chasemapper_config["doa_confidence_threshold"],
    )

    # Set speed gate for car position object
    car_track.heading_gate_threshold = chasemapper_config["car_speed_gate"]
    car_track.turn_rate_threshold = chasemapper_config["turn_rate_threshold"]

    # Start listeners using the default profile selection.
    start_listeners(
        chasemapper_config["profiles"][chasemapper_config["selected_profile"]]
    )

    # Start up the predictor, if enabled.
    if chasemapper_config["pred_enabled"]:
        initPredictor()

    # Read in last known position, if enabled

    if chasemapper_config["reload_last_position"]:
        logging.info("Read in last position requested")
        try:
            handle_new_payload_position(read_last_balloon_telemetry(), False)
        except Exception as e:
            logging.warning("Unable to read in last position")
    else:
        logging.debug("Read in last position not requested")

    # Start up the data age monitor thread.
    _data_age_monitor = Thread(target=check_data_age)
    _data_age_monitor.start()

    # Start the airspace/TFR background cache refresh threads.
    try:
        airspace_cache.start_background_refresh()
    except Exception as e:
        logging.warning("Could not start airspace cache: %s", e)

    # Start the chase-car day-track cache (loads today's points if any).
    try:
        car_track_cache.start()
    except Exception as e:
        logging.warning("Could not start car track cache: %s", e)

    # Run the Flask app, which will block until CTRL-C'd.
    logging.info(
        "Starting Chasemapper Server on: http://%s:%d/"
        % (chasemapper_config["flask_host"], chasemapper_config["flask_port"])
    )
    try:
        socketio.run(
            app,
            host=chasemapper_config["flask_host"],
            port=chasemapper_config["flask_port"],
            allow_unsafe_werkzeug=True
        )
    except TypeError as e:
        print(e)
        logging.debug("Not using allow_unsafe_werkzeug argument.")
        socketio.run(
            app,
            host=chasemapper_config["flask_host"],
            port=chasemapper_config["flask_port"]
        ) 

    # Close the predictor and data age monitor threads.
    predictor_thread_running = False
    data_monitor_thread_running = False

    # Close the chase logger
    if chase_logger:
        chase_logger.close()

    if online_uploader != None:
        online_uploader.close()

    # Attempt to close the running listeners.
    for _thread in data_listeners:
        try:
            _thread.close()
        except Exception as e:
            logging.error("Error closing thread - %s" % str(e))
