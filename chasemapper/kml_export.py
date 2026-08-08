#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#

"""
Post-flight KML export.

Bundles a whole flight into one KML that opens straight into Google
Earth for the debrief:

  * the launch-time predicted path - the *first* prediction run logged
    for each payload - plus its predicted burst and landing points,
  * the path actually flown, rebuilt from the logged APRS telemetry,
    with launch / max-altitude / last-packet placemarks,
  * the active profile's geofence, drawn as a ground footprint and a
    wall rising to its ceiling altitude,
  * every configured KML overlay (the eclipse path of totality),
    inlined so the export is a single self-contained file.

Flight data comes from the chase log (log_files/*.log - one JSON object
per line, see chasemapper.logger), not from the in-memory
current_payloads. Two reasons: an export still works after a restart,
and the log is the only place the *original* launch prediction survives
- current_payloads only ever holds the most recent run.

Track points are emitted in log order. handle_new_payload_position()
discards telemetry that isn't newer than the last track point before it
reaches the logger, so the logged sequence is already monotonic in time
and needs no re-sorting here.
"""

import logging
import os
from xml.etree import ElementTree as _ETSerialise

import defusedxml.ElementTree as ET


LOG_TELEMETRY = "BALLOON TELEMETRY"
LOG_PREDICTION = "PREDICTION"

KML_NS = "http://www.opengis.net/kml/2.2"
_KML_NSMAP = {"kml": KML_NS}

# Emit inlined overlay content with the conventional prefixes rather
# than ElementTree's ns0/ns1, so gx:Track and friends survive the copy.
_ETSerialise.register_namespace("", KML_NS)
_ETSerialise.register_namespace("gx", "http://www.google.com/kml/ext/2.2")
_ETSerialise.register_namespace("atom", "http://www.w3.org/2005/Atom")

# Document-level children of a source overlay that describe the document
# itself rather than its content. We supply our own folder name, so drop
# these instead of letting a stray <name> shadow it.
_OVERLAY_SKIP_TAGS = {
    "name",
    "description",
    "open",
    "visibility",
    "Snippet",
    "snippet",
    "LookAt",
    "Camera",
}

# Refuse to inline anything larger than this. Overlays are hand-curated
# in the config, so this only guards against a mistyped path pointing at
# something enormous.
MAX_OVERLAY_BYTES = 16 * 1024 * 1024

# KML colours are aabbggrr, not the aarrggbb you would expect.
_C_ACTUAL = "ff2626dc"  # #dc2626 red
_C_ACTUAL_FILL = "552626dc"
_C_PRED = "ff0b9ef5"  # #f59e0b amber
_C_PRED_FILL = "550b9ef5"
_C_FENCE_IN = "ffeb6325"  # #2563eb blue
_C_FENCE_IN_FILL = "33eb6325"
_C_FENCE_OUT = "ff2626dc"
_C_FENCE_OUT_FILL = "332626dc"
_C_LAUNCH = "ff4aa316"  # #16a34a green


# ---- Small XML helpers -------------------------------------------------


def _esc(value):
    """XML-escape a value for use as element text."""
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _indent(lines, levels=1):
    pad = "  " * levels
    return [pad + line for line in lines]


def _coords(points):
    """Format (lat, lon, alt) triples as a KML lon,lat,alt coordinate list."""
    return " ".join(
        "%.6f,%.6f,%.1f" % (float(lon), float(lat), float(alt))
        for (lat, lon, alt) in points
    )


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _triple(value):
    """Coerce a logged [lat, lon, alt] list into a float triple, or None."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    lat = _as_float(value[0])
    lon = _as_float(value[1])
    alt = _as_float(value[2])
    if lat is None or lon is None:
        return None
    return (lat, lon, alt if alt is not None else 0.0)


def _folder(name, body, visible=True, open_=False):
    if not body:
        return []
    # KML's Feature sequence is name, visibility, open, ... - Google Earth
    # doesn't care, but schema validators do.
    return (
        [
            "<Folder>",
            "  <name>%s</name>" % _esc(name),
            "  <visibility>%d</visibility>" % (1 if visible else 0),
            "  <open>%d</open>" % (1 if open_ else 0),
        ]
        + _indent(body)
        + ["</Folder>"]
    )


def _point_placemark(name, style_id, point, description=""):
    lat, lon, alt = point
    return [
        "<Placemark>",
        "  <name>%s</name>" % _esc(name),
        "  <description>%s</description>" % _esc(description),
        "  <styleUrl>#%s</styleUrl>" % style_id,
        "  <Point>",
        "    <altitudeMode>absolute</altitudeMode>",
        "    <coordinates>%s</coordinates>" % _coords([(lat, lon, alt)]),
        "  </Point>",
        "</Placemark>",
    ]


def _track_placemark(name, style_id, points, description=""):
    """A 3D flight path: extruded down to the ground so the shape reads
    as a wall rather than a line floating in space."""
    return [
        "<Placemark>",
        "  <name>%s</name>" % _esc(name),
        "  <description>%s</description>" % _esc(description),
        "  <styleUrl>#%s</styleUrl>" % style_id,
        "  <LineString>",
        "    <extrude>1</extrude>",
        "    <altitudeMode>absolute</altitudeMode>",
        "    <coordinates>%s</coordinates>" % _coords(points),
        "  </LineString>",
        "</Placemark>",
    ]


# ---- Styles ------------------------------------------------------------


def _styles():
    def line_style(style_id, colour, fill, width):
        return [
            '<Style id="%s">' % style_id,
            "  <LineStyle><color>%s</color><width>%d</width></LineStyle>"
            % (colour, width),
            "  <PolyStyle><color>%s</color></PolyStyle>" % fill,
            "</Style>",
        ]

    def icon_style(style_id, colour, scale=1.1):
        return [
            '<Style id="%s">' % style_id,
            "  <IconStyle><color>%s</color><scale>%.1f</scale></IconStyle>"
            % (colour, scale),
            "</Style>",
        ]

    out = []
    out += line_style("actualPath", _C_ACTUAL, _C_ACTUAL_FILL, 3)
    out += line_style("predPath", _C_PRED, _C_PRED_FILL, 3)
    out += line_style("fenceInside", _C_FENCE_IN, _C_FENCE_IN_FILL, 2)
    out += line_style("fenceOutside", _C_FENCE_OUT, _C_FENCE_OUT_FILL, 2)
    out += icon_style("launchPin", _C_LAUNCH)
    out += icon_style("actualPin", _C_ACTUAL)
    out += icon_style("predPin", _C_PRED)
    return out


# ---- Log parsing -------------------------------------------------------


def collect_flight_data(log_entries, callsigns=None):
    """Group chase-log entries by payload callsign.

    `callsigns` optionally restricts the result to a set of payloads,
    matched case-insensitively (config upper-cases them, APRS-IS does
    not always). Pass None for every payload in the log.

    Returns {callsign: {"track": [(time, lat, lon, alt), ...],
                        "prediction": {...} or None}}.

    "prediction" is the first PREDICTION run logged for that payload -
    i.e. the launch prediction, since the predictor needs two track
    points before it will run at all.

    Chase-car positions are logged without a callsign under a different
    log_type, so they never land here.
    """
    wanted = None
    if callsigns is not None:
        wanted = {str(c).strip().upper() for c in callsigns if str(c).strip()}

    flights = {}

    def _slot(call):
        return flights.setdefault(call, {"track": [], "prediction": None})

    for entry in log_entries:
        if not isinstance(entry, dict):
            continue

        _call = entry.get("callsign")
        if not _call:
            continue
        if wanted is not None and str(_call).upper() not in wanted:
            continue

        _type = entry.get("log_type")

        if _type == LOG_TELEMETRY:
            lat = _as_float(entry.get("lat"))
            lon = _as_float(entry.get("lon"))
            alt = _as_float(entry.get("alt"))
            if lat is None or lon is None:
                continue
            _slot(_call)["track"].append(
                (entry.get("time") or entry.get("log_time") or "", lat, lon, alt or 0.0)
            )

        elif _type == LOG_PREDICTION:
            slot = _slot(_call)
            if slot["prediction"] is not None:
                # Already have the launch run; later runs are the
                # predictor converging, which this export doesn't carry.
                continue
            path = [_triple(p) for p in (entry.get("pred_path") or [])]
            path = [p for p in path if p is not None]
            if len(path) < 2:
                continue
            slot["prediction"] = {
                "time": entry.get("log_time") or "",
                "path": path,
                "landing": _triple(entry.get("pred_landing")),
                "burst": _triple(entry.get("burst")),
            }

    # Drop payloads that only ever produced a prediction and no track.
    return {call: data for call, data in flights.items() if data["track"]}


def summarise_payloads(log_entries):
    """Per-payload stats, for the export picker in the web client.

    Lets the operator see what is actually in a log before choosing what
    to export - a chase log picks up every balloon the APRS-IS filter
    matched, not only the ones this profile flew.
    """
    flights = collect_flight_data(log_entries)

    payloads = []
    for call in sorted(flights):
        track = flights[call]["track"]
        payloads.append(
            {
                "callsign": call,
                "points": len(track),
                "prediction": flights[call]["prediction"] is not None,
                "first_time": track[0][0],
                "last_time": track[-1][0],
                "max_alt": max(point[3] for point in track),
            }
        )

    return payloads


def _flight_folder(callsign, data):
    """Build the per-payload folder: actual path, then launch prediction."""
    body = []

    track = data["track"]
    points = [(lat, lon, alt) for (_time, lat, lon, alt) in track]

    actual = _track_placemark(
        "Flown path (%d packets)" % len(points),
        "actualPath",
        points,
        "Reconstructed from logged APRS telemetry.",
    )

    first = track[0]
    actual += _point_placemark(
        "Launch",
        "launchPin",
        (first[1], first[2], first[3]),
        "First packet: %s, %.0f m." % (first[0], first[3]),
    )

    # Max altitude stands in for burst. Only meaningful once the payload
    # has actually climbed - a ground-only log has its "peak" at noise
    # level - and pointless if the peak is the last packet anyway.
    peak_idx = max(range(len(track)), key=lambda i: track[i][3])
    peak = track[peak_idx]
    if peak[3] > first[3] + 100.0 and peak_idx != len(track) - 1:
        actual += _point_placemark(
            "Max altitude",
            "actualPin",
            (peak[1], peak[2], peak[3]),
            "%.0f m at %s." % (peak[3], peak[0]),
        )

    last = track[-1]
    actual += _point_placemark(
        "Last packet",
        "actualPin",
        (last[1], last[2], last[3]),
        "%s, %.0f m. Not necessarily the landing point - just the last "
        "telemetry received." % (last[0], last[3]),
    )

    body += _folder("Actual path (APRS)", actual, open_=True)

    prediction = data["prediction"]
    if prediction:
        start_alt = prediction["path"][0][2]
        pred = _track_placemark(
            "Predicted path",
            "predPath",
            prediction["path"],
            "First prediction run of the flight, made at %s from %.0f m."
            % (prediction["time"], start_alt),
        )
        if prediction["burst"]:
            pred += _point_placemark(
                "Predicted burst",
                "predPin",
                prediction["burst"],
                "%.0f m." % prediction["burst"][2],
            )
        if prediction["landing"]:
            pred += _point_placemark(
                "Predicted landing",
                "predPin",
                prediction["landing"],
                "As predicted at %s." % prediction["time"],
            )
        body += _folder("Launch prediction", pred)

    return _folder("Flight - %s" % callsign, body, open_=True)


# ---- Geofence ----------------------------------------------------------


def _geofence_folder(geofence, profile_name=""):
    """Footprint on the ground plus a wall up to the ceiling altitude.

    KML can't extrude a polygon between two altitudes, so the volume is
    approximated with a clamped footprint and a ring at max_alt extruded
    down to the ground.
    """
    if not geofence:
        return []

    polygon = geofence.get("polygon") or []
    if len(polygon) < 3:
        return []

    remain = str(geofence.get("remain") or "inside").lower()
    min_alt = _as_float(geofence.get("min_alt"))
    max_alt = _as_float(geofence.get("max_alt"))
    style_id = "fenceOutside" if remain == "outside" else "fenceInside"

    # Stored rings are open; KML wants the first vertex repeated.
    ring = [(float(lat), float(lon)) for lat, lon in polygon]
    ring.append(ring[0])

    description = "Remain %s. Min alt %s m, max alt %s m." % (
        remain,
        "unset" if min_alt is None else "%.0f" % min_alt,
        "unset" if max_alt is None else "%.0f" % max_alt,
    )

    body = [
        "<Placemark>",
        "  <name>Footprint</name>",
        "  <description>%s</description>" % _esc(description),
        "  <styleUrl>#%s</styleUrl>" % style_id,
        "  <Polygon>",
        "    <tessellate>1</tessellate>",
        "    <altitudeMode>clampToGround</altitudeMode>",
        "    <outerBoundaryIs><LinearRing>",
        "      <coordinates>%s</coordinates>"
        % _coords([(lat, lon, 0.0) for lat, lon in ring]),
        "    </LinearRing></outerBoundaryIs>",
        "  </Polygon>",
        "</Placemark>",
    ]

    if max_alt is not None and max_alt > 0:
        body += [
            "<Placemark>",
            "  <name>Wall to %.0f m</name>" % max_alt,
            "  <description>%s</description>" % _esc(description),
            "  <styleUrl>#%s</styleUrl>" % style_id,
            "  <LineString>",
            "    <extrude>1</extrude>",
            "    <altitudeMode>absolute</altitudeMode>",
            "    <coordinates>%s</coordinates>"
            % _coords([(lat, lon, max_alt) for lat, lon in ring]),
            "  </LineString>",
            "</Placemark>",
        ]

    name = "Geofence - %s" % profile_name if profile_name else "Geofence"
    return _folder(name, body)


# ---- Overlay inlining --------------------------------------------------


def _strip_kml_ns(elem):
    """Drop the KML namespace from a subtree, in place.

    The elements get re-serialised inside our own <kml> document, which
    already declares KML as the default namespace, so bare tags inherit
    it. Without this every inlined element repeats an xmlns declaration.
    Tags from other namespaces (gx:Track and friends) are left qualified
    so they keep their prefix.
    """
    prefix = "{%s}" % KML_NS
    for node in elem.iter():
        if isinstance(node.tag, str) and node.tag.startswith(prefix):
            node.tag = node.tag[len(prefix):]


def _overlay_content(path):
    """Read a KML overlay and return its content elements as XML strings.

    Everything under the source <Document> is carried over except the
    document's own metadata, so shared <Style> definitions referenced by
    <styleUrl> keep resolving.
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        logging.warning("KML export - could not stat overlay %s: %s" % (path, e))
        return []

    if size > MAX_OVERLAY_BYTES:
        logging.warning(
            "KML export - overlay %s is %d bytes, skipping." % (path, size)
        )
        return []

    try:
        with open(path, "rb") as f:
            root = ET.fromstring(f.read())
    except Exception as e:
        # OSError, ParseError, or one of defusedxml's entity/DTD refusals.
        logging.warning("KML export - could not parse overlay %s: %s" % (path, e))
        return []

    container = root.find("kml:Document", _KML_NSMAP)
    if container is None:
        container = root.find("Document")
    if container is None:
        container = root  # bare <Placemark>/<Folder> under <kml>

    out = []
    for child in container:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag in _OVERLAY_SKIP_TAGS:
            continue
        try:
            _strip_kml_ns(child)
            out.append(_ETSerialise.tostring(child, encoding="unicode").strip())
        except Exception as e:  # pragma: no cover - malformed subtree
            logging.warning(
                "KML export - could not serialise %s from %s: %s" % (tag, path, e)
            )

    return out


def _overlay_folders(overlays):
    """Returns (lines, folder_count)."""
    folders = []
    count = 0
    for overlay in overlays or []:
        path = overlay.get("path")
        if not path or not os.path.isfile(path):
            logging.warning(
                "KML export - overlay '%s' missing at %s, skipping."
                % (overlay.get("name", "?"), path)
            )
            continue
        content = _overlay_content(path)
        if not content:
            continue
        folders += _folder(
            overlay.get("name") or os.path.basename(path),
            content,
            visible=bool(overlay.get("visible", True)),
        )
        count += 1
    return folders, count


# ---- Document assembly -------------------------------------------------


def build_flight_kml(
    log_entries,
    geofence=None,
    profile_name="",
    overlays=(),
    log_name="",
    generated="",
    callsigns=None,
):
    """Assemble the export. Returns (kml_string, summary_dict).

    `summary` reports what actually made it in, so the caller can refuse
    to serve an export with nothing in it.
    """
    flights = collect_flight_data(log_entries, callsigns=callsigns)

    body = []
    for call in sorted(flights):
        body += _flight_folder(call, flights[call])

    fence_folder = _geofence_folder(geofence, profile_name)
    body += fence_folder

    overlay_folders, overlay_count = _overlay_folders(overlays)
    body += overlay_folders

    title = "ChaseMapper flight - %s" % (log_name or "export")
    description = "\n".join(
        [
            "Source log: %s" % (log_name or "unknown"),
            "Generated: %s" % (generated or "unknown"),
            "Payloads: %s" % (", ".join(sorted(flights)) or "none"),
            "Profile: %s" % (profile_name or "unknown"),
        ]
    )

    lines = (
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<kml xmlns="%s">' % KML_NS,
            "<Document>",
            "  <name>%s</name>" % _esc(title),
            "  <open>1</open>",
            "  <description>%s</description>" % _esc(description),
        ]
        + _indent(_styles())
        + _indent(body)
        + ["</Document>", "</kml>", ""]
    )

    summary = {
        "callsigns": sorted(flights),
        "points": sum(len(f["track"]) for f in flights.values()),
        "predictions": sum(1 for f in flights.values() if f["prediction"]),
        "geofence": bool(fence_folder),
        "overlays": overlay_count,
    }

    return "\n".join(lines), summary
