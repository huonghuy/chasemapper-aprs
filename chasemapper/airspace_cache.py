#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#

"""
Server-side cache for Spanish airspace overlays.

Fetches ICAO airspace for the northern half of Spain from ENAIRE (the Spanish
ANSP) and serves cached GeoJSON to the chasemapper frontend. Filters to a
regional bounding box, persists to disk, and refreshes in the background.

Source is the ENAIRE "Aero VIGOR" ArcGIS FeatureServer, layer 41 (ESPACIOS
AEREOS), which is AIXM-derived and carries the full ICAO attribute set:

    https://servais.enaire.es/insignia/rest/services/INSIGNIA_SRV/
        Aero_SRV_VIGOR_V2/FeatureServer/41

Note there is no temporary-restriction layer here. ENAIRE's NOTAM service
requires an API token, so activation of the D/R/TSA/TRA areas below is NOT
reflected. Those areas are drawn unconditionally, which is the conservative
choice, but NOTAMs must still be checked separately before flight.
"""

import html
import json
import logging
import os
import threading
import time

import requests


# Northern half of mainland Spain, plus enough margin for a chase to drift.
REGION_BBOX = {"lat_min": 39.5, "lat_max": 44.5, "lon_min": -10.0, "lon_max": 4.5}
AIRSPACE_REFRESH_SEC = 12 * 60 * 60
CACHE_DIR = os.path.join("cache", "airspace")
REQUEST_TIMEOUT = 60

# ENAIRE publishes on the AIRAC cycle (28 days), so a day-old cache is fine.
STALE_THRESHOLD_SEC = 24 * 60 * 60

_AIRSPACE_URL = (
    "https://servais.enaire.es/insignia/rest/services/INSIGNIA_SRV/"
    "Aero_SRV_VIGOR_V2/FeatureServer/41/query"
)

# ENAIRE TYPE_CODE values grouped into the layers the UI exposes. The "-P"
# suffixed codes are the sector subdivisions of their parent area (e.g. TMA-P
# "TGAL-1" is part of the Galicia TMA) and carry real vertical limits, so they
# belong with the parent type.
#
# Deliberately excluded, because they would blanket the map or are not
# restrictions a balloon can bust:
#   FIR, FIR-A, FIR-P, UIR, OCA, NAS, FRA, FRA-P, SRR, RVSMTA - region-scale
#   ATCSMA  - ATC surveillance minimum altitudes (a vectoring aid, not a volume)
#   DEL     - delegation of airspace between ATC units (administrative)
#   SECTOR, FREQ, LFR, REDUCCION_V - VFR sectors / frequency areas
#   ENR_5_5, ZRVF, PROTECT - sporting, photographic and nature areas, all
#                            capped around 400-1000 ft AGL
_LAYER_TYPES = {
    "ctr": ("CTR", "CTR-P"),
    "tma": ("TMA", "TMA-P"),
    "cta": ("CTA", "CTA-P"),
    "atz": ("ATZ", "FIZ"),
    "restricted": ("D", "R", "P", "Prohibido_Sobrevuelo", "PROHIBIDO VFR"),
    "military": ("TSA", "TRA"),
    "rmz_tmz": ("RMZ", "TMZ"),
}

# The layer set is defined once, by _LAYER_TYPES. Deriving the served list from
# it means a layer can never be exposed without a type mapping behind it.
LAYERS = tuple(_LAYER_TYPES)

# Vertical limits come from the DISTVERT{LOWER,UPPER}_* triplet, NOT from
# LOWER_VAL / UPPER_VAL. Those two look like altitudes in feet but are render
# sort keys: ENAIRE adds a flat +12200 ft to every ground-referenced value, so
# CTR BILBAO (1000 ft AGL) reports UPPER_VAL 13200. Verified against the whole
# northern-Spain set - the offset is exactly 12200 for the ALT/HEIG/HEI datums
# and 0 for STD/HEIS/HEISG. Using them as altitudes understates nothing but
# wildly overstates low-level airspace, so they are ignored here.
_UOM_TO_FEET = {"FT": 1.0, "M": 3.28084, "FL": 100.0}

# Reference datum per ENAIRE's own rendering of each code in NIVEL_INF/NIVEL_SUP:
#   STD -> "FL245",  HEIS -> "ft AMSL",  ALT -> "ft ALT",
#   HEIG -> "ft AGL"/"SFC",  HEI/HEISG -> bare "ft"/"SFC"
_DATUM_BY_CODE = {
    "STD": "FL",
    "HEIS": "AMSL",
    "ALT": "AMSL",
    "HEIG": "AGL",
    "HEI": "AGL",
    "HEISG": "AGL",
}

# ICAO operating-hour abbreviations used in WORKHR_CODE.
_WORKHR_LABELS = {
    "H24": "Continuous (H24)",
    "HJ": "Sunrise to sunset (HJ)",
    "HN": "Sunset to sunrise (HN)",
    "NOTAM": "Activated by NOTAM",
    "HR ATS": "During ATS hours",
    "HR AD": "During aerodrome hours",
    # RMK means "see the remark", which WORKHRRMK_TXT already supplies.
    "RMK": "",
}

_started = False
_start_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_in_progress = False


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


# Pre-compute all cache paths from the LAYERS constant so that file paths
# at runtime are always looked up from this dict, never derived from user input.
_LAYER_PATHS = {
    layer: (
        os.path.join(CACHE_DIR, layer + ".geojson"),
        os.path.join(CACHE_DIR, layer + ".meta.json"),
    )
    for layer in LAYERS
}


def _layer_paths(layer):
    paths = _LAYER_PATHS.get(layer)
    if paths is None:
        raise ValueError("unknown layer: %s" % layer)
    return paths


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _write_layer(layer, geojson, fetched_at):
    geo_path, meta_path = _layer_paths(layer)
    _atomic_write_json(geo_path, geojson)
    _atomic_write_json(
        meta_path,
        {
            "fetched_at": fetched_at,
            "feature_count": len(geojson.get("features", [])),
        },
    )


def _layer_cached(layer):
    """Is this layer on disk? Deliberately does not parse the GeoJSON."""
    geo_path, _ = _layer_paths(layer)
    return os.path.exists(geo_path)


def _read_meta(layer):
    """The layer's small sidecar metadata, or None. Does not touch the GeoJSON."""
    _, meta_path = _layer_paths(layer)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception as e:
        logging.warning("Airspace cache: failed to read %s metadata: %s", layer, e)
        return None


def _bbox_geometry_param():
    b = REGION_BBOX
    return "{},{},{},{}".format(b["lon_min"], b["lat_min"], b["lon_max"], b["lat_max"])


def _geometry_intersects_region(geom):
    """Return True when a GeoJSON geometry's bounds overlap the region."""
    if not geom:
        return False
    coords = geom.get("coordinates")
    if coords is None:
        return False

    points = []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            if len(c) >= 2:
                points.append((c[0], c[1]))
            return
        if isinstance(c, (list, tuple)):
            for child in c:
                walk(child)

    walk(coords)
    if not points:
        return False

    lon_min = min(point[0] for point in points)
    lon_max = max(point[0] for point in points)
    lat_min = min(point[1] for point in points)
    lat_max = max(point[1] for point in points)
    b = REGION_BBOX
    return not (
        lon_max < b["lon_min"]
        or lon_min > b["lon_max"]
        or lat_max < b["lat_min"]
        or lat_min > b["lat_max"]
    )


def _clean_text(value):
    """Trim, collapse whitespace and unescape HTML entities.

    ENAIRE returns HTML-escaped text ("&amp;") and pads several fields with
    leading whitespace, so raw values are not display-ready.
    """
    if value is None:
        return ""
    text = html.unescape(str(value))
    return " ".join(text.split())


def _vertical_limit(props, side):
    """Vertical limit for side ("LOWER"/"UPPER") as (feet, datum).

    Returns (None, "") when the limit is not specified. Feet are exact for the
    AMSL and FL datums; an AGL value cannot be converted to AMSL without terrain
    elevation, so the datum is reported alongside and must not be ignored.
    """
    raw = props.get("DISTVERT%s_VAL" % side)
    uom = _clean_text(props.get("DISTVERT%s_UOM" % side)).upper()
    code = _clean_text(props.get("DISTVERT%s_CODE" % side)).upper()

    if raw is None or uom not in _UOM_TO_FEET:
        return None, ""

    try:
        feet = float(raw) * _UOM_TO_FEET[uom]
    except (TypeError, ValueError):
        return None, ""

    return feet, _DATUM_BY_CODE.get(code, "")


def _format_schedule(props):
    """Human-readable operating hours from WORKHR_CODE / WORKHRRMK_TXT."""
    code = _clean_text(props.get("WORKHR_CODE")).upper()
    remark = _clean_text(props.get("WORKHRRMK_TXT"))

    label = _WORKHR_LABELS.get(code, code)
    if label and remark and label.lower() != remark.lower():
        return "%s - %s" % (label, remark)
    return label or remark


def _zone_flags(props):
    """Names of the mandatory-zone flags set on a feature (RMZ/TMZ/FPMZ/FBZ)."""
    return [
        flag
        for flag in ("RMZ", "TMZ", "FPMZ", "FBZ")
        if _clean_text(props.get(flag)) == "1"
    ]


def _normalise_feature(feature):
    """Map ENAIRE's AIXM field names onto a stable set the frontend can rely on.

    Raw fields are left in place; the normalised keys are added alongside.
    """
    props = dict(feature.get("properties") or {})

    type_code = _clean_text(props.get("TYPE_CODE"))
    ident = _clean_text(props.get("IDENT_TXT"))
    name = _clean_text(props.get("NAME_TXT")) or ident or type_code

    frequency = _clean_text(props.get("FREQTRANS_VAL"))
    freq_uom = _clean_text(props.get("FREQ_UOM"))
    if frequency and freq_uom.upper() == "MHZ":
        frequency = "%s MHz" % frequency

    lower_ft, lower_datum = _vertical_limit(props, "LOWER")
    upper_ft, upper_datum = _vertical_limit(props, "UPPER")

    props.update(
        {
            "name": name,
            "ident": ident,
            "type_code": type_code,
            "airspace_class": _clean_text(props.get("CLASS")),
            # NIVEL_INF/NIVEL_SUP are ENAIRE's own display strings and are the
            # authoritative human-readable limits ("SFC", "1000ft AGL", "FL145").
            "lower": _clean_text(props.get("NIVEL_INF")),
            "upper": _clean_text(props.get("NIVEL_SUP")),
            "lower_ft": lower_ft,
            "lower_datum": lower_datum,
            "upper_ft": upper_ft,
            "upper_datum": upper_datum,
            "schedule": _format_schedule(props),
            "frequency": frequency,
            "remarks": _clean_text(props.get("REMARKS_TXT")),
            "zones": _zone_flags(props),
        }
    )

    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": props,
    }


def _fetch_airspace(layer):
    """Fetch and normalise one layer's airspace from ENAIRE."""
    types = _LAYER_TYPES[layer]
    where = "TYPE_CODE IN (%s)" % ",".join("'%s'" % t for t in types)

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
        "resultRecordCount": 4000,
    }
    r = requests.get(_AIRSPACE_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    raw = r.json()

    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        snippet = json.dumps(raw)[:300] if isinstance(raw, (dict, list)) else repr(raw)[:300]
        raise ValueError("unexpected response shape for {}: {}".format(layer, snippet))

    # ArcGIS signals truncation rather than failing, so surface it loudly.
    if raw.get("exceededTransferLimit") or (raw.get("properties") or {}).get(
        "exceededTransferLimit"
    ):
        logging.warning(
            "Airspace cache: %s hit the server record limit, results are truncated", layer
        )

    features = []
    for item in raw.get("features", []):
        try:
            geom = item.get("geometry")
            if not geom:
                continue
            # The bbox is applied server-side; re-check so a change in the
            # upstream query parameters cannot silently widen the region.
            if not _geometry_intersects_region(geom):
                continue
            features.append(_normalise_feature(item))
        except Exception as e:
            logging.debug("Airspace cache: skipping malformed %s feature: %s", layer, e)
            continue

    return {"type": "FeatureCollection", "features": features}


def _refresh_layer(layer):
    fetched_at = time.time()
    geo = _fetch_airspace(layer)

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


def get_layer_path(layer):
    """Absolute path to the cached GeoJSON, or None if unknown or not cached.

    The file on disk is already exactly what the frontend asks for, so callers
    serve it verbatim rather than parsing and re-serialising it.
    """
    if layer not in LAYERS:
        return None
    geo_path, _ = _layer_paths(layer)
    if not os.path.exists(geo_path):
        return None
    return os.path.abspath(geo_path)


def get_status():
    now = time.time()
    out = {}
    for layer in LAYERS:
        meta = _read_meta(layer)
        cached = _layer_cached(layer)
        fetched_at = meta.get("fetched_at") if meta else None
        feature_count = meta.get("feature_count") if meta else 0
        age_seconds = (now - fetched_at) if fetched_at else None
        stale = age_seconds is not None and age_seconds > STALE_THRESHOLD_SEC
        out[layer] = {
            "cached": cached,
            "fetched_at": fetched_at,
            "age_seconds": age_seconds,
            "feature_count": feature_count,
            "stale": stale,
        }
    return out


def _refresh_layers_parallel(layers):
    """Refresh several layers at once. Returns {layer: succeeded}.

    One thread per layer, so a round costs one request timeout rather than one
    per layer. ENAIRE is slow enough that doing this serially is minutes.
    """
    # Pre-populate so a worker that outlives the join timeout still
    # leaves a (False) entry rather than a missing key.
    results = {layer: False for layer in layers}
    threads = []

    def worker(layer):
        results[layer] = _try_refresh(layer)

    for layer in layers:
        t = threading.Thread(target=worker, args=(layer,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=REQUEST_TIMEOUT + 5)

    return results


def force_refresh_all():
    """Re-fetch every layer from ENAIRE now. Runs layers in parallel; serialised
    with a global lock so concurrent button presses coalesce into one round.
    Returns a result dict with per-layer success flags and the post-refresh status."""
    global _refresh_in_progress
    with _refresh_lock:
        if _refresh_in_progress:
            return {"already_running": True, "status": get_status()}
        _refresh_in_progress = True

    try:
        results = _refresh_layers_parallel(LAYERS)
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

    missing = [layer for layer in LAYERS if not _layer_cached(layer)]
    cached = [layer for layer in LAYERS if layer not in missing]
    if cached:
        logging.info("Airspace cache: loading %s from cache", ", ".join(cached))
    if missing:
        # Blocks startup, so the layers go out together rather than one at a
        # time - serially this is up to REQUEST_TIMEOUT per layer.
        logging.info(
            "Airspace cache: no cache for %s, fetching synchronously", ", ".join(missing)
        )
        _refresh_layers_parallel(missing)

    for layer in LAYERS:
        threading.Thread(
            target=_refresh_loop, args=(layer, AIRSPACE_REFRESH_SEC), daemon=True
        ).start()
