#!/usr/bin/env python3
#
#   CHASE - Browser-Based Chase Mapper
#
#   Build KML overlays of a solar eclipse's path of totality.
#
#   Source data is the NASA/GSFC path table by Fred Espenak, which lists the
#   northern limit, southern limit and central line of the Moon's umbral shadow
#   (WGS 84) at 120-second intervals, along with the central-line duration, sun
#   altitude/azimuth and path width:
#
#       https://eclipse.gsfc.nasa.gov/SEpath/SEpath2001/SE2026Aug12Tpath.html
#
#   Defaults are set up for the 2026 Aug 12 total eclipse over northern Spain.
#   Writes two overlays: the full path, and a subset covering the Atlantic
#   approach and Iberia (the full path crosses the pole, which Web Mercator
#   cannot draw sensibly).
#
#   Usage:
#       python3 utils/eclipse_path_kml.py --output-dir overlays/
#
import argparse
import html
import logging
import math
import os
import re
import sys

import requests

DEFAULT_URL = "https://eclipse.gsfc.nasa.gov/SEpath/SEpath2001/SE2026Aug12Tpath.html"
DEFAULT_ECLIPSE = "Total Solar Eclipse of 2026 Aug 12"

# Launch site, annotated on the regional overlay. Lat, lon, name.
DEFAULT_SITE = (42.46139, -4.10713, "Sordillos, Spain")

# Central-line latitude at which the regional overlay starts.
REGIONAL_START_LAT = 60.0

# IUGG mean Earth radius, in metres.
EARTH_RADIUS_M = 6371008.8

# Colours as CSS #rrggbb. kml_colour() converts to KML's aabbggrr byte order.
COLOUR_CENTRAL = "#e63946"
COLOUR_LIMIT = "#f4a300"
COLOUR_BAND = "#4338ca"
COLOUR_TIME = "#e63946"
COLOUR_SITE = "#0aa04b"

_COORD = r"(\d{1,3})\s+(\d{1,2}\.\d)([NSEW])"
_ROW = re.compile(
    r"^\s*(?P<time>\d{2}:\d{2}|Limits)\s+"
    + _COORD + r"\s+" + _COORD + r"\s+"     # northern limit lat, lon
    + _COORD + r"\s+" + _COORD + r"\s+"     # southern limit lat, lon
    + _COORD + r"\s+" + _COORD + r"\s+"     # central line   lat, lon
    r"(?P<ratio>\d\.\d{3})\s+"
    r"(?P<alt>\d+|-)\s+(?P<azm>\d+|-)\s+"
    r"(?P<width>\d+)\s+"
    r"(?P<dur>\d+m\d+\.\d+s)\s*$"
)


def dm_to_degrees(degrees, minutes, hemisphere):
    """ Degrees + decimal minutes + hemisphere -> signed decimal degrees. """
    _value = int(degrees) + float(minutes) / 60.0
    return -_value if hemisphere in ("S", "W") else _value


def parse_path_table(text):
    """ Parse a NASA/GSFC eclipse path table into a list of row dicts. """
    _text = html.unescape(re.sub(r"<[^>]+>", "", text))

    _rows = []
    for _line in _text.splitlines():
        _match = _ROW.match(_line)
        if _match is None:
            continue

        # Groups 1..18 are the six (degrees, minutes, hemisphere) triples.
        _g = _match.groups()
        _coords = [dm_to_degrees(_g[i], _g[i + 1], _g[i + 2]) for i in range(1, 19, 3)]

        _rows.append(
            {
                "time": _match.group("time"),
                "north": (_coords[0], _coords[1]),
                "south": (_coords[2], _coords[3]),
                "central": (_coords[4], _coords[5]),
                "sun_alt": None if _match.group("alt") == "-" else int(_match.group("alt")),
                "width_km": int(_match.group("width")),
                "duration": _match.group("dur"),
            }
        )

    return _rows


def _angular_distance(lat1, lon1, lat2, lon2):
    """ Haversine central angle, in radians. Arguments are already in radians. """
    _h = (
        math.sin((lat2 - lat1) / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2.0) ** 2
    )
    return 2.0 * math.asin(math.sqrt(_h))


def great_circle_point(start, end, fraction):
    """ Interpolate along the great circle between two (lat, lon) points. """
    _lat1, _lon1, _lat2, _lon2 = [
        math.radians(v) for v in (start[0], start[1], end[0], end[1])
    ]

    _d = _angular_distance(_lat1, _lon1, _lat2, _lon2)
    if _d == 0.0:
        return start

    _a = math.sin((1.0 - fraction) * _d) / math.sin(_d)
    _b = math.sin(fraction * _d) / math.sin(_d)

    _x = _a * math.cos(_lat1) * math.cos(_lon1) + _b * math.cos(_lat2) * math.cos(_lon2)
    _y = _a * math.cos(_lat1) * math.sin(_lon1) + _b * math.cos(_lat2) * math.sin(_lon2)
    _z = _a * math.sin(_lat1) + _b * math.sin(_lat2)

    return (
        math.degrees(math.atan2(_z, math.hypot(_x, _y))),
        math.degrees(math.atan2(_y, _x)),
    )


def densify(points, steps=8):
    """ Insert great-circle points between samples so lines curve correctly. """
    if len(points) < 2:
        return list(points)

    _out = []
    for _start, _end in zip(points, points[1:]):
        for _i in range(steps):
            _out.append(great_circle_point(_start, _end, _i / float(steps)))
    _out.append(points[-1])
    return _out


def great_circle_distance(a, b):
    """ Great-circle distance between two (lat, lon) points, in metres. """
    _lat1, _lon1, _lat2, _lon2 = [
        math.radians(v) for v in (a[0], a[1], b[0], b[1])
    ]
    return EARTH_RADIUS_M * _angular_distance(_lat1, _lon1, _lat2, _lon2)


def closest_approach(site, rows, key):
    """ Closest approach of a densified path line to site.

    Returns (distance_m, index of the segment's first row).
    """
    _best = (float("inf"), 0)

    for _i, (_a, _b) in enumerate(zip(rows, rows[1:])):
        for _step in range(201):
            _f = _step / 200.0
            _d = great_circle_distance(site, great_circle_point(_a[key], _b[key], _f))
            if _d < _best[0]:
                _best = (_d, _i)

    return _best


def kml_colour(css_colour, alpha=255):
    """ CSS #rrggbb -> KML aabbggrr. """
    _hex = css_colour.lstrip("#")
    return "%02x%s%s%s" % (alpha, _hex[4:6], _hex[2:4], _hex[0:2])


def _escape(text):
    return html.escape(str(text), quote=False)


def _coord_block(points):
    return " ".join("%.6f,%.6f,0" % (lon, lat) for lat, lon in points)


def line_placemark(name, description, points, colour, width):
    return """  <Placemark>
    <name>%s</name>
    <description>%s</description>
    <Style>
      <LineStyle><color>%s</color><width>%s</width></LineStyle>
    </Style>
    <LineString>
      <tessellate>1</tessellate>
      <coordinates>%s</coordinates>
    </LineString>
  </Placemark>
""" % (
        _escape(name),
        _escape(description),
        kml_colour(colour),
        width,
        _coord_block(points),
    )


def band_placemark(name, description, north, south, colour, alpha):
    # Northern limit forward, southern limit reversed, closed back to the start.
    _ring = list(north) + list(reversed(south)) + [north[0]]
    return """  <Placemark>
    <name>%s</name>
    <description>%s</description>
    <Style>
      <LineStyle><color>00000000</color><width>0</width></LineStyle>
      <PolyStyle><color>%s</color></PolyStyle>
    </Style>
    <Polygon>
      <tessellate>1</tessellate>
      <outerBoundaryIs><LinearRing>
        <coordinates>%s</coordinates>
      </LinearRing></outerBoundaryIs>
    </Polygon>
  </Placemark>
""" % (
        _escape(name),
        _escape(description),
        kml_colour(colour, alpha),
        _coord_block(_ring),
    )


def point_placemark(name, description, point, colour, size="small"):
    # IconStyle for Google Earth; the simplestyle ExtendedData keys are what
    # leaflet-omnivore actually surfaces, since it does not parse IconStyle.
    return """  <Placemark>
    <name>%s</name>
    <description>%s</description>
    <Style>
      <IconStyle><color>%s</color></IconStyle>
    </Style>
    <ExtendedData>
      <Data name="marker-color"><value>%s</value></Data>
      <Data name="marker-size"><value>%s</value></Data>
    </ExtendedData>
    <Point><coordinates>%.6f,%.6f,0</coordinates></Point>
  </Placemark>
""" % (
        _escape(name),
        _escape(description),
        kml_colour(colour),
        _escape(colour),
        _escape(size),
        point[1],
        point[0],
    )


def build_kml(doc_name, doc_description, rows, site=None, site_stats=None):
    """ Build a KML document for the supplied path rows. """
    _north = densify([r["north"] for r in rows])
    _south = densify([r["south"] for r in rows])
    _central = densify([r["central"] for r in rows])

    _parts = [
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>%s</name>
  <description>%s</description>
"""
        % (_escape(doc_name), _escape(doc_description))
    ]

    _parts.append(
        band_placemark(
            "Path of totality",
            "Umbral shadow track. Anywhere inside this band sees a total eclipse.",
            _north,
            _south,
            COLOUR_BAND,
            0x40,
        )
    )
    _parts.append(
        line_placemark(
            "Northern limit",
            "Northern edge of totality. North of this line the eclipse is partial only.",
            _north,
            COLOUR_LIMIT,
            2,
        )
    )
    _parts.append(
        line_placemark(
            "Southern limit",
            "Southern edge of totality. South of this line the eclipse is partial only.",
            _south,
            COLOUR_LIMIT,
            2,
        )
    )
    _parts.append(
        line_placemark(
            "Central line",
            "Maximum duration of totality along the path.",
            _central,
            COLOUR_CENTRAL,
            3,
        )
    )

    for _row in rows:
        if _row["time"] == "Limits":
            continue
        _parts.append(
            point_placemark(
                "%s UT" % _row["time"],
                "Shadow centre at %s UT. Totality %s, path width %d km, sun %s deg altitude."
                % (
                    _row["time"],
                    _row["duration"],
                    _row["width_km"],
                    _row["sun_alt"],
                ),
                _row["central"],
                COLOUR_TIME,
            )
        )

    if site is not None and site_stats is not None:
        _parts.append(
            point_placemark(
                site[2],
                site_stats,
                (site[0], site[1]),
                COLOUR_SITE,
                size="large",
            )
        )

    _parts.append("</Document>\n</kml>\n")
    return "".join(_parts)


def inside_band(site, rows):
    """ Is the site within the umbral band?

    Ray casting over the band ring in plain lat/lon. Valid because no eclipse
    path segment handled here crosses the antimeridian.
    """
    _ring = densify([r["north"] for r in rows]) + list(
        reversed(densify([r["south"] for r in rows]))
    )

    _lat, _lon = site[0], site[1]
    _inside = False
    for (_lat1, _lon1), (_lat2, _lon2) in zip(_ring, _ring[1:] + _ring[:1]):
        if (_lon1 > _lon) != (_lon2 > _lon):
            _lat_at_crossing = _lat1 + (_lon - _lon1) / (_lon2 - _lon1) * (_lat2 - _lat1)
            if _lat < _lat_at_crossing:
                _inside = not _inside

    return _inside


def describe_site(site, rows):
    """ Human-readable summary of the site's position relative to the path. """
    _central_d, _idx = closest_approach(site, rows, "central")
    _north_d, _ = closest_approach(site, rows, "north")
    _south_d, _ = closest_approach(site, rows, "south")

    _before = rows[_idx]
    _after = rows[_idx + 1]

    _inside = inside_band(site, rows)

    return (
        "%s. %s totality. %.1f km from the central line; %.0f km inside the "
        "northern limit and %.0f km inside the southern limit. Shadow centre passes "
        "between %s and %s UT, central-line duration %s to %s, sun %s to %s deg "
        "altitude. Distances are to the 120-second sampled path."
        % (
            "%.5f, %.5f" % (site[0], site[1]),
            "Inside" if _inside else "OUTSIDE",
            _central_d / 1000.0,
            _north_d / 1000.0,
            _south_d / 1000.0,
            _before["time"],
            _after["time"],
            _before["duration"],
            _after["duration"],
            _before["sun_alt"],
            _after["sun_alt"],
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="NASA/GSFC path table URL")
    parser.add_argument("--input", default=None, help="Local path table HTML instead of --url")
    parser.add_argument("--output-dir", default="overlays", help="Directory to write KML into")
    parser.add_argument("--eclipse", default=DEFAULT_ECLIPSE, help="Eclipse name for the KML title")
    parser.add_argument(
        "--site",
        default="%f,%f,%s" % DEFAULT_SITE,
        help="Launch site to annotate, as lat,lon,name. Empty string to omit.",
    )
    args = parser.parse_args()

    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

    if args.input:
        _text = open(args.input, encoding="utf-8", errors="replace").read()
        _source = args.input
    else:
        logging.info("Fetching %s", args.url)
        _resp = requests.get(args.url, timeout=60)
        _resp.raise_for_status()
        _text = _resp.text
        _source = args.url

    _rows = parse_path_table(_text)
    if len(_rows) < 3:
        logging.critical("Parsed only %d rows - has the table format changed?", len(_rows))
        return 1
    logging.info("Parsed %d path rows.", len(_rows))

    _site = None
    if args.site.strip():
        _lat, _lon, _name = args.site.split(",", 2)
        _site = (float(_lat), float(_lon), _name)

    _provenance = "%s. Path data: NASA/GSFC (Fred Espenak), %s. WGS 84, sampled at 120-second intervals." % (
        args.eclipse,
        _source,
    )

    os.makedirs(args.output_dir, exist_ok=True)

    # Full path.
    _full_path = os.path.join(args.output_dir, "eclipse_2026_path_full.kml")
    with open(_full_path, "w", encoding="utf-8") as _f:
        _f.write(
            build_kml(
                "%s - full path" % args.eclipse,
                _provenance
                + " The path crosses the north pole, which Web Mercator cannot draw"
                " sensibly - use the regional overlay for chase planning.",
                _rows,
            )
        )
    logging.info("Wrote %s", _full_path)

    # Regional subset: from REGIONAL_START_LAT down to the end of the path.
    _start = next(
        (i for i, r in enumerate(_rows) if r["central"][0] <= REGIONAL_START_LAT),
        0,
    )
    _regional = _rows[_start:]
    logging.info(
        "Regional subset: %d rows, %s to %s UT.",
        len(_regional),
        _regional[0]["time"],
        _regional[-1]["time"],
    )

    _site_stats = None
    if _site is not None:
        _site_stats = describe_site(_site, _regional)
        logging.info("Site: %s", _site_stats)

    # The table's final row is the sunset extreme limit, which can sit a long way
    # past the last 120-second sample. Say so rather than implying the drawn band
    # is equally accurate along its whole length.
    _gaps = [
        (great_circle_distance(a["central"], b["central"]) / 1000.0, a["time"], b["time"])
        for a, b in zip(_regional, _regional[1:])
    ]
    _worst = max(_gaps)
    logging.info("Largest central-line gap: %.0f km (%s to %s)", *_worst)
    _regional_note = (
        " Samples are joined along great circles; spacing widens toward sunset, "
        "reaching %.0f km between %s and %s, so the band is coarsest at its "
        "south-eastern end." % _worst
    )

    _regional_path = os.path.join(args.output_dir, "eclipse_2026_path_iberia.kml")
    with open(_regional_path, "w", encoding="utf-8") as _f:
        _f.write(
            build_kml(
                "%s - Atlantic approach and Iberia" % args.eclipse,
                _provenance + _regional_note,
                _regional,
                site=_site,
                site_stats=_site_stats,
            )
        )
    logging.info("Wrote %s", _regional_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
