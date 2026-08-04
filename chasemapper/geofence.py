#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#

"""
Per-profile geofence storage and KML parsing.

A geofence is a polygon (lat/lon ring) plus min/max altitude and a
remain-inside / remain-outside flag. Geofences are uploaded as KML
files exported by the HAB Bounder cut-down device, or drawn directly
on the Leaflet map; either way they're stored as a small JSON-friendly
dict and persisted to a sidecar JSON file next to the chasemapper
config. The frontend renders the active profile's polygon on the map.

Store shape on disk:

    {
        "profiles": { "<profile>": {polygon, min_alt, max_alt, remain}, ... },
        "trash":    [ {profile, geofence, deleted_at}, ... ]
    }

Anything moved out of `profiles` (via DELETE, or by being overwritten
by a new upload/draw) lands in `trash` so the operator can recover
from accidental clears. The trash is auto-pruned after 2 days — long
enough to recover from same-flight mistakes, short enough that the
sidecar stays small. Definitive per-flight history lives on choppies
itself, not here.

KML shape we expect (HAB Bounder export):

    <Placemark>
      <name>Geofence</name>
      <description>
        Remain inside
        Min Alt: -500 meters
        Max Alt: 50000 meters
      </description>
      <Polygon>
        <outerBoundaryIs><LinearRing>
          <coordinates>
            lon,lat,alt
            ...
          </coordinates>
        </LinearRing></outerBoundaryIs>
      </Polygon>
    </Placemark>

The exporter may also include a <gx:Track> with the flight path; we
ignore everything that isn't the geofence Placemark.
"""

import json
import logging
import os
import re
import threading
import time
import defusedxml.ElementTree as ET


KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}

# Cap on how big a single uploaded KML may be. Geofence-only KML is
# tiny; this limit really only protects us from someone uploading a
# multi-MB Flight.KML by mistake (the geofence is still parsed fine
# from those — but no point loading 50 MB of track points to discard).
MAX_KML_BYTES = 5 * 1024 * 1024

_save_lock = threading.Lock()


class GeofenceParseError(Exception):
    """Raised when a KML upload cannot be parsed into a geofence."""


def _close_ring(polygon):
    """Strip repeated trailing vertices in place, then require 3 remaining.

    Bounder exports sometimes repeat the closing waypoint, and hand-drawn
    rings can double a vertex on a slow click. Leaflet closes the ring
    itself, so the explicit close is redundant either way. Both entry
    points share this so KML and on-map drawing agree on what a valid ring is.
    """
    while len(polygon) > 3 and polygon[-1] == polygon[-2]:
        polygon.pop()
    if len(polygon) > 3 and polygon[0] == polygon[-1]:
        polygon.pop()
    if len(polygon) < 3:
        raise GeofenceParseError(
            "Polygon needs at least 3 distinct vertices (got %d)." % len(polygon)
        )


def _findall_ns(elem, tag):
    """Find children regardless of whether the doc declares the KML
    namespace. Bounder's KML does declare it, but be lenient."""
    return elem.findall("kml:" + tag, KML_NS) + elem.findall(tag)


def _find_ns(elem, path_with_kml_prefix, fallback_path):
    """Try a namespaced path first, fall back to non-namespaced."""
    found = elem.find(path_with_kml_prefix, KML_NS)
    if found is not None:
        return found
    return elem.find(fallback_path)


def parse_kml_geofence(kml_bytes):
    """Parse a HAB Bounder KML and return the geofence dict.

    Returned shape (matches what the frontend expects):

        {
            "polygon": [[lat, lon], [lat, lon], ...],  # open ring
            "min_alt": float,                          # meters
            "max_alt": float,                          # meters
            "remain":  "inside" | "outside",
        }

    Raises GeofenceParseError on any malformed input.
    """
    if not kml_bytes:
        raise GeofenceParseError("Empty upload.")

    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as e:
        logging.debug("KML parse error: %s", e)
        raise GeofenceParseError("Invalid XML: could not parse document.")

    # Find every Placemark, then pick one that contains a Polygon. If
    # there are several, prefer the one named "Geofence" (case
    # insensitive) — that's what the Bounder exports.
    placemarks = root.findall(".//kml:Placemark", KML_NS)
    if not placemarks:
        placemarks = root.findall(".//Placemark")

    chosen = None
    for pm in placemarks:
        polygon_el = _find_ns(pm, ".//kml:Polygon", ".//Polygon")
        if polygon_el is None:
            continue
        name_el = _find_ns(pm, "kml:name", "name")
        name = (name_el.text or "").strip().lower() if name_el is not None else ""
        if "geofence" in name:
            chosen = pm
            break
        if chosen is None:
            chosen = pm  # remember first, keep looking for a "Geofence"

    if chosen is None:
        raise GeofenceParseError("No <Polygon> Placemark found in KML.")

    coords_el = _find_ns(
        chosen,
        ".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates",
        ".//outerBoundaryIs/LinearRing/coordinates",
    )
    if coords_el is None or not (coords_el.text or "").strip():
        raise GeofenceParseError("Polygon has no <coordinates>.")

    polygon = []
    for tok in coords_el.text.split():
        # Each token is "lon,lat[,alt]". KML is lon-first; Leaflet
        # wants lat-first. Drop altitude (we carry min/max separately).
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
            raise GeofenceParseError(
                "Coordinate out of range: lon=%s lat=%s" % (lon, lat)
            )
        polygon.append([lat, lon])

    _close_ring(polygon)

    # Description carries Remain inside/outside + Min/Max altitude.
    desc_el = _find_ns(chosen, "kml:description", "description")
    desc = (desc_el.text or "") if desc_el is not None else ""

    remain = "inside"
    m = re.search(r"Remain\s+(inside|outside)", desc, re.I)
    if m:
        remain = m.group(1).lower()

    def _parse_alt(label_re, default):
        m = re.search(label_re + r"\s*:\s*(-?\d+(?:\.\d+)?)", desc, re.I)
        return float(m.group(1)) if m else default

    # Defaults match the HAB Bounder spec when the Altitude line is
    # omitted from the config: -1000 m to 50000 m.
    min_alt = _parse_alt(r"Min\s+Alt", -1000.0)
    max_alt = _parse_alt(r"Max\s+Alt", 50000.0)

    return {
        "polygon": polygon,
        "min_alt": min_alt,
        "max_alt": max_alt,
        "remain": remain,
    }



def build_geofence_from_polygon(polygon, min_alt, max_alt, remain):
    """Validate a user-drawn polygon and return the canonical geofence
    dict (same shape parse_kml_geofence produces).

    `polygon` is expected as an iterable of [lat, lon] pairs.
    """
    if not isinstance(polygon, (list, tuple)):
        raise GeofenceParseError("polygon must be a list of [lat, lon] pairs.")

    cleaned = []
    for pt in polygon:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            raise GeofenceParseError("Each polygon point must be [lat, lon].")
        try:
            lat = float(pt[0])
            lon = float(pt[1])
        except (TypeError, ValueError):
            raise GeofenceParseError("Polygon point has non-numeric coordinate.")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise GeofenceParseError(
                "Coordinate out of range: lat=%s lon=%s" % (lat, lon)
            )
        cleaned.append([lat, lon])

    _close_ring(cleaned)

    try:
        min_alt_f = float(min_alt)
        max_alt_f = float(max_alt)
    except (TypeError, ValueError):
        raise GeofenceParseError("min_alt and max_alt must be numeric.")
    if min_alt_f >= max_alt_f:
        raise GeofenceParseError("min_alt must be less than max_alt.")

    remain_norm = str(remain or "").strip().lower()
    if remain_norm not in ("inside", "outside"):
        raise GeofenceParseError("remain must be 'inside' or 'outside'.")

    return {
        "polygon": cleaned,
        "min_alt": min_alt_f,
        "max_alt": max_alt_f,
        "remain": remain_norm,
    }


# How long a cleared/overwritten geofence stays in the trash before
# the next save sweeps it away. Long enough to recover from same-flight
# mistakes; the authoritative per-flight log lives on choppies.
TRASH_TTL_SECONDS = 2 * 24 * 60 * 60


def _empty_store():
    return {"profiles": {}, "trash": []}


def _normalize_store(data):
    """Coerce on-disk data into the {profiles, trash} shape. Migrates
    the pre-trash flat shape ({profile: geofence, ...}) transparently."""
    if not isinstance(data, dict):
        return _empty_store()

    if "profiles" in data or "trash" in data:
        profiles = data.get("profiles") or {}
        trash = data.get("trash") or []
        if not isinstance(profiles, dict):
            profiles = {}
        if not isinstance(trash, list):
            trash = []
        return {"profiles": profiles, "trash": trash}

    # Legacy flat shape: every top-level value is a geofence dict.
    profiles = {k: v for k, v in data.items() if isinstance(v, dict)}
    return {"profiles": profiles, "trash": []}


def load_store(path):
    """Load the geofences sidecar. Returns an empty store on
    missing/invalid input. Always returns the {profiles, trash} shape."""
    if not path or not os.path.isfile(path):
        return _empty_store()
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logging.warning("Could not read geofence store %s: %s" % (path, e))
        return _empty_store()
    return _normalize_store(data)


def save_store(path, store):
    """Atomically write the geofences sidecar. Prunes expired trash
    entries on the way out so the file doesn't grow unbounded."""
    if not path:
        return
    prune_trash(store)
    with _save_lock:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(store, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            logging.error("Could not save geofence store %s: %s" % (path, e))


def prune_trash(store, now=None):
    """Drop trash entries older than TRASH_TTL_SECONDS. Mutates `store`
    in place and returns the number of entries removed."""
    trash = store.get("trash")
    if not isinstance(trash, list) or not trash:
        return 0
    cutoff = (now if now is not None else time.time()) - TRASH_TTL_SECONDS
    kept = []
    dropped = 0
    for entry in trash:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        deleted_at = entry.get("deleted_at")
        try:
            deleted_at_f = float(deleted_at)
        except (TypeError, ValueError):
            # No / unparseable timestamp — treat as already-expired.
            dropped += 1
            continue
        if deleted_at_f < cutoff:
            dropped += 1
            continue
        kept.append(entry)
    if dropped:
        store["trash"] = kept
    return dropped


def _push_to_trash(store, profile, geofence):
    """Move a profile's existing geofence into the trash. No-op if the
    profile has nothing set or the geofence is empty."""
    if not geofence:
        return
    store.setdefault("trash", []).append({
        "profile": profile,
        "geofence": geofence,
        "deleted_at": time.time(),
    })


def set_profile_geofence(store, profile, geofence):
    """Replace a profile's geofence, sending any previous one to trash.
    Mutates `store` in place."""
    profiles = store.setdefault("profiles", {})
    previous = profiles.get(profile)
    if previous and previous != geofence:
        _push_to_trash(store, profile, previous)
    profiles[profile] = geofence


def clear_profile_geofence(store, profile):
    """Soft-delete a profile's geofence (moves it to trash). Returns
    True if something was cleared."""
    profiles = store.setdefault("profiles", {})
    previous = profiles.pop(profile, None)
    if not previous:
        return False
    _push_to_trash(store, profile, previous)
    return True


def restore_latest(store, profile):
    """Pop the most recent trash entry for `profile` and reinstate it
    as the active geofence. Returns the restored geofence dict, or
    None if there was nothing to restore.

    If the profile already has a geofence set, that one is pushed to
    trash first so the restore is itself undoable.
    """
    trash = store.setdefault("trash", [])
    # Most recent first.
    for i in range(len(trash) - 1, -1, -1):
        entry = trash[i]
        if not isinstance(entry, dict):
            continue
        if entry.get("profile") != profile:
            continue
        restored = entry.get("geofence")
        if not restored:
            continue
        del trash[i]
        profiles = store.setdefault("profiles", {})
        current = profiles.get(profile)
        if current and current != restored:
            _push_to_trash(store, profile, current)
        profiles[profile] = restored
        return restored
    return None


def has_trash(store, profile):
    """True if there is at least one recoverable trash entry for the
    given profile."""
    for entry in store.get("trash", []) or []:
        if isinstance(entry, dict) and entry.get("profile") == profile and entry.get("geofence"):
            return True
    return False


def attach_to_profiles(chasemapper_config, store):
    """Stamp each profile dict with its geofence (or None) and a
    has_trash flag so the frontend can enable Restore. Shipped out via
    /get_config and server_settings_update."""
    profiles_store = store.get("profiles", {}) if isinstance(store, dict) else {}
    for name, profile in chasemapper_config.get("profiles", {}).items():
        profile["geofence"] = profiles_store.get(name)
        profile["geofence_has_trash"] = has_trash(store, name)
