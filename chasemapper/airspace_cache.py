#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#

"""
Server-side cache for FAA airspace + TFR overlays.

Fetches Class B/C/D/E, Special Use Airspace, and TFRs from FAA endpoints,
filters to a regional bounding box (MD/PA/DE/VA/WV), persists to disk, and
serves cached GeoJSON to the chasemapper frontend. Background threads refresh
the cache (12h for airspace, 15min for TFRs).
"""

import json
import logging
import os
import tempfile
import threading
import time

import requests


REGION_BBOX = {"lat_min": 36.5, "lat_max": 42.5, "lon_min": -83.7, "lon_max": -74.6}
AIRSPACE_REFRESH_SEC = 12 * 60 * 60
TFR_REFRESH_SEC = 15 * 60
CACHE_DIR = os.path.join("cache", "airspace")
REQUEST_TIMEOUT = 30

# maxRecordCount on both FAA FeatureServers. The page cap is a runaway guard:
# 25 pages is ~50k features, far beyond anything the region can hold.
_ARCGIS_PAGE_SIZE = 2000
_ARCGIS_MAX_PAGES = 25

STALE_THRESHOLD_SEC = {
    "class_b": 24 * 60 * 60,
    "class_c": 24 * 60 * 60,
    "class_d": 24 * 60 * 60,
    "class_e": 24 * 60 * 60,
    "sua": 24 * 60 * 60,
    "tfr": 60 * 60,
}

_CLASS_AIRSPACE_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Class_Airspace/FeatureServer/0/query"
)
_SUA_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Special_Use_Airspace/FeatureServer/0/query"
)
_TFR_WFS_URL = "https://tfr.faa.gov/geoserver/TFR/ows"

LAYERS = ("class_b", "class_c", "class_d", "class_e", "sua", "tfr")

_CLASS_WHERE = {
    "class_b": "LOCAL_TYPE='CLASS_B'",
    "class_c": "LOCAL_TYPE='CLASS_C'",
    "class_d": "LOCAL_TYPE='CLASS_D'",
    # Class E is split across CLASS_E and CLASS_E2 through CLASS_E6 in the
    # FAA layer. Filtering on LOCAL_TYPE='CLASS_E' returns only the broad
    # en-route area and omits the useful surface/transition airspace.
    "class_e": "CLASS='E'",
}

# The backend owns FAA-schema normalization. buildAirspacePopup reads only the
# lowercase names emitted here (plus notam_id, derived from NOTAM_KEY below),
# so a vendor field rename is a one-line change in this table.
#
# The WFS feed carries exactly: GID, CNS_LOCATION_ID, NOTAM_KEY, TITLE,
# LAST_MODIFICATION_DATETIME, STATE, LEGAL. Notably there is no expiry field,
# so TFR popups cannot show one.
_TFR_PROP_ALIASES = {
    "type": "LEGAL",
    "description": "TITLE",
    "last_modified": "LAST_MODIFICATION_DATETIME",
}

_started = False
_start_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_in_progress = False
_LAYER_WRITE_LOCKS = {layer: threading.Lock() for layer in LAYERS}


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)
    # _stage_json cleans up after its own failures, but a hard crash mid-write
    # strands a temp file. They have unique names, so sweep at startup (before
    # any writer is running) rather than letting them accumulate. .meta.json
    # sidecars are left over from the pre-single-file layout.
    for name in os.listdir(CACHE_DIR):
        if name.endswith(".tmp") or name.endswith(".meta.json"):
            _discard(os.path.join(CACHE_DIR, name))


# Pre-compute all cache paths from the LAYERS constant so that file paths
# at runtime are always looked up from this dict, never derived from user input.
_LAYER_PATHS = {layer: os.path.join(CACHE_DIR, layer + ".geojson") for layer in LAYERS}

# Publication metadata per layer, mirroring what is on disk. Held in memory
# because the only other way to read it is to parse the whole
# FeatureCollection back off disk, and get_status wants it for all six layers
# at once. None means "nothing cached"; a missing key means "not looked up yet".
_meta_cache = {}


def _layer_path(layer):
    path = _LAYER_PATHS.get(layer)
    if path is None:
        raise ValueError("unknown layer: %s" % layer)
    return path


def _discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _stage_json(path, data):
    """Serialise data to a unique temp file beside path; return the temp path.

    The temp name must be unique, not "<path>.tmp": force_refresh_all abandons
    workers that outlive its join timeout, so a second refresh round can be
    writing the same layer while the first still is. Sharing one temp path lets
    the second worker's os.replace promote a file the first is mid-write,
    leaving truncated JSON as the live cache.
    """
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except Exception:
        _discard(tmp)
        raise
    return tmp


def _meta_from(geojson):
    """Publication metadata carried inside a cached FeatureCollection."""
    return {
        "fetched_at": geojson.get("fetched_at"),
        "feature_count": geojson.get(
            "feature_count", len(geojson.get("features") or [])
        ),
    }


def _write_layer(layer, geojson, fetched_at):
    """Publish a layer as one self-describing file, atomically.

    fetched_at and feature_count travel inside the FeatureCollection as
    GeoJSON foreign members (RFC 7946 s6.1 — consumers ignore unknown
    members) rather than in a sidecar. A sidecar takes two os.replace calls
    to publish, and there is no ordering that makes that safe: a reader
    landing between them, or a failure on the second, sees new geometry
    described by the previous fetch's metadata while _try_refresh logs
    "keeping stale cache". One file means one replace, so the layer and its
    provenance can never disagree.
    """
    path = _layer_path(layer)
    geojson = dict(geojson)
    geojson["fetched_at"] = fetched_at
    geojson["feature_count"] = len(geojson["features"])

    # Serialises two refresh rounds racing on one layer.
    with _LAYER_WRITE_LOCKS[layer]:
        tmp = _stage_json(path, geojson)
        try:
            os.replace(tmp, path)
        except Exception:
            _discard(tmp)
            raise
        _meta_cache[layer] = _meta_from(geojson)


def _read_layer(layer):
    path = _layer_path(layer)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logging.warning("Airspace cache: failed to read %s: %s", layer, e)
        return None


def _layer_meta(layer):
    """Cached metadata for a layer, or None when nothing is cached.

    Populating takes the layer's write lock so a concurrent _write_layer
    can't have its fresher entry overwritten by this slower read.
    """
    if layer not in _meta_cache:
        with _LAYER_WRITE_LOCKS[layer]:
            if layer not in _meta_cache:
                geo = _read_layer(layer)
                _meta_cache[layer] = _meta_from(geo) if geo is not None else None
    return _meta_cache[layer]


def _bbox_geometry_param():
    b = REGION_BBOX
    return "{},{},{},{}".format(b["lon_min"], b["lat_min"], b["lon_max"], b["lat_max"])


def _get_geojson(url, params):
    """Fetch a GeoJSON FeatureCollection, failing loudly on an unexpected shape.

    Every layer enters the system through here, so upstream validation happens
    once at the fetch boundary rather than per-fetcher.
    """
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    raw = r.json()
    if (
        not isinstance(raw, dict)
        or raw.get("type") != "FeatureCollection"
        or not isinstance(raw.get("features"), list)
    ):
        snippet = repr(raw)[:300]
        raise ValueError("unexpected response shape from {}: {}".format(url, snippet))
    return raw


def _fetch_arcgis(url, where):
    """Query an FAA ArcGIS FeatureServer layer, clipped to the region bbox.

    Pages through the result set. Both FeatureServers cap a single response at
    maxRecordCount (2000 today) and announce the cut only via
    properties.exceededTransferLimit — nested there, not at the top level,
    because the output format is geojson. Unpaged, a widened REGION_BBOX or a
    densified FAA layer would cache a truncated FeatureCollection that is
    indistinguishable from a complete one: well-formed, plausible feature
    count, no stale flag, incomplete airspace on the map.
    """
    params = {
        "where": where,
        "geometry": _bbox_geometry_param(),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
        "resultRecordCount": _ARCGIS_PAGE_SIZE,
        # Paging is only coherent under a stable sort. OBJECTID is present on
        # both the Class_Airspace and Special_Use_Airspace layers.
        "orderByFields": "OBJECTID",
        # 5 decimal places is ~1m on the ground — far finer than anything
        # visible on a chase map, and roughly halves the payload. Class E
        # alone is tens of MB at full precision.
        "geometryPrecision": 5,
    }

    features = []
    for page in range(_ARCGIS_MAX_PAGES):
        raw = _get_geojson(url, dict(params, resultOffset=page * _ARCGIS_PAGE_SIZE))
        features.extend(raw["features"])
        if not (raw.get("properties") or {}).get("exceededTransferLimit"):
            return {"type": "FeatureCollection", "features": features}

    # Raise rather than return short: _try_refresh then keeps the stale cache
    # and logs, which beats silently publishing a partial layer.
    raise ValueError(
        "{} still reports more records after {} pages ({} features)".format(
            url, _ARCGIS_MAX_PAGES, len(features)
        )
    )


def _fetch_class_airspace(layer):
    return _fetch_arcgis(_CLASS_AIRSPACE_URL, _CLASS_WHERE[layer])


def _fetch_sua():
    return _fetch_arcgis(_SUA_URL, "1=1")


def _geometry_intersects_region(geom):
    """Return True when a GeoJSON geometry's bounds overlap the region."""
    if not geom:
        return False
    coords = geom.get("coordinates")
    if coords is None:
        return False

    lons, lats = [], []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            if len(c) >= 2:
                lons.append(c[0])
                lats.append(c[1])
            return
        if isinstance(c, (list, tuple)):
            for child in c:
                walk(child)

    walk(coords)
    if not lons:
        return False

    b = REGION_BBOX
    return (
        min(lons) <= b["lon_max"]
        and max(lons) >= b["lon_min"]
        and min(lats) <= b["lat_max"]
        and max(lats) >= b["lat_min"]
    )


def _fetch_tfrs():
    """Fetch active TFR polygons from the WFS feed used by the FAA map."""
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "TFR:V_TFR_LOC",
        "maxFeatures": 1000,
        "outputFormat": "application/json",
        "srsname": "EPSG:4326",
    }
    raw = _get_geojson(_TFR_WFS_URL, params)

    features = []
    for item in raw.get("features", []):
        try:
            geom = item.get("geometry")
            if not geom:
                continue
            if not _geometry_intersects_region(geom):
                continue

            props = dict(item.get("properties") or {})
            notam_key = props.get("NOTAM_KEY")
            if notam_key:
                props.setdefault("notam_id", str(notam_key).split("-", 1)[0])
            for alias, source in _TFR_PROP_ALIASES.items():
                props.setdefault(alias, props.get(source))
            features.append({"type": "Feature", "geometry": geom, "properties": props})
        except Exception as e:
            logging.debug("Airspace cache: skipping malformed TFR item: %s", e)
            continue

    return {"type": "FeatureCollection", "features": features}


def _refresh_layer(layer):
    fetched_at = time.time()
    if layer in _CLASS_WHERE:
        geo = _fetch_class_airspace(layer)
    elif layer == "sua":
        geo = _fetch_sua()
    elif layer == "tfr":
        geo = _fetch_tfrs()
    else:
        raise ValueError("unknown layer: " + layer)

    _write_layer(layer, geo, fetched_at)
    logging.info(
        "Airspace cache: refreshed %s (%d features)",
        layer,
        len(geo.get("features", [])),
    )


def _try_refresh(layer):
    try:
        _refresh_layer(layer)
        return True
    except Exception as e:
        logging.warning("Airspace cache: refresh failed for %s: %s (keeping stale cache)", layer, e)
        return False


def _refresh_loop(layer, interval_sec):
    while True:
        time.sleep(interval_sec)
        _try_refresh(layer)


def get_layer_geojson(layer):
    """The cached FeatureCollection, or None. Carries fetched_at/feature_count."""
    if layer not in LAYERS:
        return None
    return _read_layer(layer)


def get_status():
    now = time.time()
    out = {}
    for layer in LAYERS:
        meta = _layer_meta(layer)
        fetched_at = meta.get("fetched_at") if meta else None
        age_seconds = (now - fetched_at) if fetched_at else None
        out[layer] = {
            "cached": meta is not None,
            "fetched_at": fetched_at,
            "age_seconds": age_seconds,
            "feature_count": meta.get("feature_count", 0) if meta else 0,
            "stale": (
                age_seconds is not None
                and age_seconds > STALE_THRESHOLD_SEC.get(layer, 24 * 60 * 60)
            ),
        }
    return out


def force_refresh_all():
    """Re-fetch every layer from FAA now. Runs layers in parallel; serialised
    with a global lock so concurrent button presses coalesce into one round.
    Returns a result dict with per-layer success flags and the post-refresh status."""
    global _refresh_in_progress
    with _refresh_lock:
        if _refresh_in_progress:
            return {"already_running": True, "status": get_status()}
        _refresh_in_progress = True

    try:
        # Pre-populate so a worker that outlives the join timeout still
        # leaves a (False) entry rather than a missing key.
        results = {layer: False for layer in LAYERS}
        threads = []

        def worker(layer):
            results[layer] = _try_refresh(layer)

        for layer in LAYERS:
            t = threading.Thread(target=worker, args=(layer,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=REQUEST_TIMEOUT + 5)

        return {"already_running": False, "results": results, "status": get_status()}
    finally:
        _refresh_in_progress = False


def start_background_refresh():
    """Idempotent. Synchronously hydrates any missing caches, then starts background threads."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    _ensure_cache_dir()

    for layer in LAYERS:
        geo = _read_layer(layer)
        # A cache written by the old sidecar layout has no embedded
        # fetched_at, so re-fetch once rather than serve it with no age.
        if geo is None or geo.get("fetched_at") is None:
            logging.info("Airspace cache: no usable cache for %s, fetching synchronously", layer)
            _try_refresh(layer)
        else:
            _meta_cache[layer] = _meta_from(geo)
            logging.info("Airspace cache: loading %s from cache", layer)

    for layer in _CLASS_WHERE:
        threading.Thread(
            target=_refresh_loop, args=(layer, AIRSPACE_REFRESH_SEC), daemon=True
        ).start()
    threading.Thread(
        target=_refresh_loop, args=("sua", AIRSPACE_REFRESH_SEC), daemon=True
    ).start()
    threading.Thread(
        target=_refresh_loop, args=("tfr", TFR_REFRESH_SEC), daemon=True
    ).start()
