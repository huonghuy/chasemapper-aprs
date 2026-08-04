#!/usr/bin/env python
#
#   CHASE - Browser-Based Chase Mapper
#
#   Copyright (C) 2026  Huy Huong <huyhuong@umd.edu>
#   Released under GNU GPL v3 or later
#

"""
Backend proxy for Spanish cadastral parcel queries (point + radius).

Spain has no single parcel registry. The state cadastre (Direccion General del
Catastro) covers most of the country, but Navarra and the three Basque
provinces keep foral regimes with their own cadastres and are absent from it.
This module routes a query to whichever service covers the point:

    Catastro   state INSPIRE WFS, everywhere except the foral territories
    Navarra    IDENA INSPIRE CP WFS
    Alava      Diputacion Foral de Alava ArcGIS
    Bizkaia    Diputacion Foral de Bizkaia ArcGIS

Gipuzkoa publishes no open parcel service, so it is a known gap and is reported
as such rather than as an error.

IMPORTANT: none of these publish owner names. Spanish law treats owner identity
as protected personal data, released only to the owner or on proof of legitimate
interest. What comes back is the parcel boundary, its official cadastral
reference and its area - enough to identify a plot exactly, but not to look up
who to contact. The Maryland layer this replaced did carry owner names; that
capability does not exist here.
"""

import logging
import math
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


REQUEST_TIMEOUT = 45
RADIUS_MAX_KM = 2.0
RADIUS_MIN_KM = 0.1
MAX_FEATURES = 3000

# Catastro rejects requests without a browser-ish User-Agent and rate-limits
# hard, resetting the connection rather than returning an error status.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

_GML_NS = "http://www.opengis.net/gml/3.2"
_CP_NS = "http://inspire.ec.europa.eu/schemas/cp/4.0"


def _build_session():
    """Session that retries on Catastro's connection resets.

    Catastro throttles by dropping the TCP connection rather than returning a
    status code, so plain requests raises ConnectionError on the first attempt
    surprisingly often. urllib3 retries connect/read errors as well as the
    listed statuses, with exponential backoff.
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


_SESSION = _build_session()


def _empty_fc(error=None, error_code=None, source=None):
    fc = {"type": "FeatureCollection", "features": []}
    if error:
        fc["error"] = error
    if error_code:
        fc["error_code"] = error_code
    if source:
        fc["source"] = source
    return fc


def _bbox_km(lat, lon, radius_km):
    """Crude lat/lon delta around a point. Good enough for small envelopes."""
    deg_lat = (radius_km * 1000.0) / 111_320.0
    deg_lon = (radius_km * 1000.0) / (
        111_320.0 * max(math.cos(math.radians(lat)), 0.01)
    )
    return {
        "lat_min": lat - deg_lat,
        "lat_max": lat + deg_lat,
        "lon_min": lon - deg_lon,
        "lon_max": lon + deg_lon,
    }


def _feature(geometry, ref="", area_m2=None, municipality="", info_url=""):
    """Build a parcel feature with the property names the frontend expects."""
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "ref": ref,
            "area_m2": area_m2,
            "municipality": municipality,
            "info_url": info_url,
        },
    }


# ---------------------------------------------------------------------------
# Catastro (state) - INSPIRE WFS, GML 3.2.1 only
# ---------------------------------------------------------------------------

_CATASTRO_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"


def _parse_poslist(text):
    """GML posList of "lat lon lat lon ..." -> GeoJSON [[lon, lat], ...].

    Catastro serves EPSG:4326 in the OGC axis order (latitude first), which is
    the reverse of GeoJSON, so the pairs are swapped here.
    """
    nums = [float(v) for v in text.split()]
    return [[nums[i + 1], nums[i]] for i in range(0, len(nums) - 1, 2)]


def _parse_catastro_gml(xml_bytes):
    """Extract parcels from a Catastro INSPIRE GML FeatureCollection."""
    root = ET.fromstring(xml_bytes)

    features = []
    for parcel in root.iter("{%s}CadastralParcel" % _CP_NS):
        rings = []
        # A parcel is a MultiSurface of Surfaces; exterior first, then any holes.
        for boundary in ("exterior", "interior"):
            for node in parcel.iter("{%s}%s" % (_GML_NS, boundary)):
                for poslist in node.iter("{%s}posList" % _GML_NS):
                    if poslist.text:
                        ring = _parse_poslist(poslist.text)
                        if len(ring) >= 4:
                            rings.append(ring)
        if not rings:
            continue

        ref = parcel.findtext("{%s}nationalCadastralReference" % _CP_NS) or ""
        area = parcel.findtext("{%s}areaValue" % _CP_NS)
        try:
            area = float(area) if area else None
        except ValueError:
            area = None

        features.append(
            _feature(
                {"type": "Polygon", "coordinates": rings},
                ref=ref.strip(),
                area_m2=area,
                info_url=(
                    "https://www1.sedecatastro.gob.es/CYCBienInmueble/"
                    "OVCListaBienes.aspx?RC=" + ref.strip()
                )
                if ref.strip()
                else "",
            )
        )

    return features


def _fetch_catastro(bbox):
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "TYPENAMES": "cp:CadastralParcel",
        "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
        # urn-form EPSG:4326 is latitude-first, so the bbox is lat,lon ordered.
        "BBOX": "{},{},{},{},urn:ogc:def:crs:EPSG::4326".format(
            bbox["lat_min"], bbox["lon_min"], bbox["lat_max"], bbox["lon_max"]
        ),
    }
    r = _SESSION.get(_CATASTRO_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return _parse_catastro_gml(r.content)


# ---------------------------------------------------------------------------
# Navarra - IDENA INSPIRE CP WFS, emits GeoJSON directly
# ---------------------------------------------------------------------------

_NAVARRA_URL = "https://inspire.navarra.es/services/CP/wfs"


def _fetch_navarra(bbox):
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "TYPENAMES": "CP:CadastralParcel",
        "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
        "BBOX": "{},{},{},{},urn:ogc:def:crs:EPSG::4326".format(
            bbox["lat_min"], bbox["lon_min"], bbox["lat_max"], bbox["lon_max"]
        ),
        "OUTPUTFORMAT": "application/json",
        "COUNT": MAX_FEATURES,
    }
    r = _SESSION.get(_NAVARRA_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    features = []
    for item in data.get("features", []):
        props = item.get("properties") or {}

        # IDENA nests these as {"value": ..., "@uom": ...} / {"localId": ...}.
        area = props.get("areaValue")
        if isinstance(area, dict):
            area = area.get("value")
        try:
            area = float(area) if area is not None else None
        except (TypeError, ValueError):
            area = None

        ref = props.get("nationalCadastralReference") or ""
        if not ref:
            inspire_id = props.get("inspireId")
            if isinstance(inspire_id, dict):
                ref = inspire_id.get("localId") or ""

        features.append(
            _feature(item.get("geometry"), ref=str(ref), area_m2=area)
        )

    return features


# ---------------------------------------------------------------------------
# Alava and Bizkaia - foral ArcGIS servers, GeoJSON out
# ---------------------------------------------------------------------------

_ALAVA_URL = (
    "https://geo.araba.eus/geoaraba/rest/services/OGC_ARABA/"
    "WFS_Katastroa/MapServer/{layer}/query"
)
# 19 = urban parcels, 23 = rustic parcels.
_ALAVA_LAYERS = (19, 23)

_BIZKAIA_URL = (
    "https://geo.bizkaia.eus/arcgisserver/rest/services/Ekonomia_Economia/"
    "INGB_Consultas/MapServer/2/query"
)


def _arcgis_params(bbox):
    return {
        "where": "1=1",
        "geometry": "{},{},{},{}".format(
            bbox["lon_min"], bbox["lat_min"], bbox["lon_max"], bbox["lat_max"]
        ),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
        "resultRecordCount": MAX_FEATURES,
    }


def _fetch_alava(bbox):
    features = []
    for layer in _ALAVA_LAYERS:
        r = _SESSION.get(
            _ALAVA_URL.format(layer=layer),
            params=_arcgis_params(bbox),
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ValueError("Alava layer %s: %s" % (layer, data["error"]))

        for item in data.get("features", []):
            props = item.get("properties") or {}
            features.append(
                _feature(
                    item.get("geometry"),
                    ref=str(props.get("REF_CATASTRAL") or ""),
                    area_m2=props.get("Shape.STArea()"),
                    municipality=str(props.get("MUNICIPIO") or ""),
                    info_url=str(props.get("INFO") or ""),
                )
            )
    return features


def _fetch_bizkaia(bbox):
    params = _arcgis_params(bbox)
    # Es_Baja marks superseded parcels. Drawing them would overlay stale
    # boundaries on the current ones (~3% of records around Bilbao).
    params["where"] = "Es_Baja = 0"
    r = _SESSION.get(_BIZKAIA_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise ValueError("Bizkaia: %s" % data["error"])

    features = []
    for item in data.get("features", []):
        props = item.get("properties") or {}
        # Bizkaia has no single reference field; compose the municipality /
        # polygon / parcel codes the foral cadastre uses to identify a plot.
        parts = [
            props.get("Codigo_Municipio"),
            props.get("Codigo_Poligono"),
            props.get("Codigo_Parcela"),
        ]
        ref = "-".join(str(p) for p in parts if p not in (None, ""))
        features.append(
            _feature(
                item.get("geometry"),
                ref=ref,
                area_m2=props.get("Shape.STArea()"),
                municipality=str(props.get("Codigo_Municipio") or ""),
            )
        )
    return features


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Bounding boxes are the extents each service advertises, used only to pick
# which service to try. Provinces are not rectangular, so a hit here is a
# candidate, not a guarantee - an empty result falls through to the next one.
PROVIDERS = [
    {
        "name": "navarra",
        "label": "Cadastre of Navarra (IDENA)",
        "bbox": {"lon_min": -2.51, "lat_min": 41.89, "lon_max": -0.71, "lat_max": 43.33},
        "fetch": _fetch_navarra,
    },
    {
        "name": "alava",
        "label": "Cadastre of Alava (Diputacion Foral)",
        "bbox": {"lon_min": -3.29, "lat_min": 42.47, "lon_max": -2.22, "lat_max": 43.22},
        "fetch": _fetch_alava,
    },
    {
        "name": "bizkaia",
        "label": "Cadastre of Bizkaia (Diputacion Foral)",
        "bbox": {"lon_min": -3.46, "lat_min": 42.98, "lon_max": -2.41, "lat_max": 43.46},
        "fetch": _fetch_bizkaia,
    },
    {
        "name": "catastro",
        "label": "Direccion General del Catastro",
        # Mainland Spain plus the islands; the foral providers above are tried
        # first because Catastro holds no data for their territories.
        "bbox": {"lon_min": -18.5, "lat_min": 26.2, "lon_max": 5.3, "lat_max": 44.8},
        "fetch": _fetch_catastro,
    },
]

# Gipuzkoa runs its own cadastre but publishes no open parcel service, so a
# blank result there is a known gap rather than a failure.
_GIPUZKOA_BBOX = {"lon_min": -2.60, "lat_min": 42.90, "lon_max": -1.72, "lat_max": 43.40}


def _in_bbox(lat, lon, bbox):
    return (
        bbox["lat_min"] <= lat <= bbox["lat_max"]
        and bbox["lon_min"] <= lon <= bbox["lon_max"]
    )


def get_parcels_near(lat, lon, radius_km):
    """Fetch cadastral parcels within radius_km of (lat, lon).

    Returns a GeoJSON FeatureCollection with ``source`` and ``source_label``
    naming the cadastre the data came from. Sets ``_truncated = True`` when the
    upstream record cap was hit. On validation or fetch failure, returns an
    empty FeatureCollection with ``error`` set.
    """
    try:
        lat = float(lat)
        lon = float(lon)
        radius_km = float(radius_km)
    except (TypeError, ValueError):
        return _empty_fc("lat/lon/radius must be numeric")

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return _empty_fc("lat/lon out of range")
    if not (RADIUS_MIN_KM <= radius_km <= RADIUS_MAX_KM):
        return _empty_fc(
            "radius must be between {} and {} km".format(RADIUS_MIN_KM, RADIUS_MAX_KM)
        )

    candidates = [p for p in PROVIDERS if _in_bbox(lat, lon, p["bbox"])]
    if not candidates:
        return _empty_fc(
            "Cadastral information is only available inside Spain.",
            "outside_coverage",
        )

    bbox = _bbox_km(lat, lon, radius_km)
    failures = []

    for provider in candidates:
        try:
            features = provider["fetch"](bbox)
        except Exception as e:
            logging.warning(
                "Parcel proxy: %s fetch failed: %s", provider["name"], e
            )
            failures.append(provider["name"])
            continue

        if not features:
            # No data here; the next candidate may cover this point.
            continue

        fc = {
            "type": "FeatureCollection",
            "features": features[:MAX_FEATURES],
            "source": provider["name"],
            "source_label": provider["label"],
        }
        if len(features) >= MAX_FEATURES:
            fc["_truncated"] = True
        return fc

    if failures and len(failures) == len(candidates):
        return _empty_fc(
            "Cadastral service is temporarily unavailable.", "upstream_unavailable"
        )

    if _in_bbox(lat, lon, _GIPUZKOA_BBOX):
        return _empty_fc(
            "Gipuzkoa keeps its own cadastre and publishes no open parcel "
            "service, so boundaries are not available here.",
            "no_open_service",
        )

    return _empty_fc(
        "No cadastral parcels are published for this location.", "no_data"
    )
