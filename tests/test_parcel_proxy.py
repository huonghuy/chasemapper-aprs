import unittest
from unittest.mock import Mock, patch

from chasemapper import parcel_proxy


# A minimal but structurally real Catastro INSPIRE response: GML 3.2.1,
# ISO-8859-1, coordinates in EPSG:4326 latitude-first order.
CATASTRO_GML = """<?xml version="1.0" encoding="ISO-8859-1"?>
<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:cp="http://inspire.ec.europa.eu/schemas/cp/4.0">
  <member>
    <cp:CadastralParcel gml:id="ES.SDGC.CP.000900100VN00B">
      <cp:areaValue uom="m2">306</cp:areaValue>
      <cp:geometry>
        <gml:MultiSurface srsName="http://www.opengis.net/def/crs/EPSG/0/4326">
          <gml:surfaceMember>
            <gml:Surface>
              <gml:patches><gml:PolygonPatch>
                <gml:exterior><gml:LinearRing>
                  <gml:posList srsDimension="2" count="4">42.4628 -4.1105 42.4629 -4.1104 42.4627 -4.1103 42.4628 -4.1105</gml:posList>
                </gml:LinearRing></gml:exterior>
              </gml:PolygonPatch></gml:patches>
            </gml:Surface>
          </gml:surfaceMember>
        </gml:MultiSurface>
      </cp:geometry>
      <cp:nationalCadastralReference>000900100VN00B</cp:nationalCadastralReference>
    </cp:CadastralParcel>
  </member>
</FeatureCollection>
""".encode("iso-8859-1")


class CatastroGmlTests(unittest.TestCase):
    def test_coordinates_are_swapped_to_lon_lat(self):
        """Catastro serves EPSG:4326 latitude-first; GeoJSON needs lon first."""
        features = parcel_proxy._parse_catastro_gml(CATASTRO_GML)

        self.assertEqual(len(features), 1)
        ring = features[0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], [-4.1105, 42.4628])
        # Longitude in Spain is small and negative, latitude is ~42; a failed
        # swap would put 42 in the longitude slot.
        for lon, lat in ring:
            self.assertLess(abs(lon), 10.0)
            self.assertGreater(lat, 35.0)

    def test_reference_area_and_link_are_extracted(self):
        props = parcel_proxy._parse_catastro_gml(CATASTRO_GML)[0]["properties"]
        self.assertEqual(props["ref"], "000900100VN00B")
        self.assertEqual(props["area_m2"], 306.0)
        self.assertIn("000900100VN00B", props["info_url"])

    def test_parcel_without_geometry_is_skipped(self):
        empty = CATASTRO_GML.replace(
            b"42.4628 -4.1105 42.4629 -4.1104 42.4627 -4.1103 42.4628 -4.1105", b""
        )
        self.assertEqual(parcel_proxy._parse_catastro_gml(empty), [])

    def test_degenerate_ring_is_rejected(self):
        # Fewer than four positions cannot close a polygon.
        stub = CATASTRO_GML.replace(
            b"42.4628 -4.1105 42.4629 -4.1104 42.4627 -4.1103 42.4628 -4.1105",
            b"42.4628 -4.1105 42.4629 -4.1104",
        )
        self.assertEqual(parcel_proxy._parse_catastro_gml(stub), [])


class RoutingTests(unittest.TestCase):
    def _providers_for(self, lat, lon):
        return [
            p["name"] for p in parcel_proxy.PROVIDERS
            if parcel_proxy._in_bbox(lat, lon, p["bbox"])
        ]

    def test_foral_providers_are_tried_before_catastro(self):
        # Catastro holds no data in the foral territories, so the regional
        # service must be attempted first.
        self.assertEqual(self._providers_for(42.8125, -1.6458)[0], "navarra")
        self.assertEqual(self._providers_for(42.8467, -2.6716)[0], "alava")
        self.assertEqual(self._providers_for(43.2630, -2.9350)[0], "bizkaia")

    def test_launch_site_routes_to_catastro_only(self):
        self.assertEqual(self._providers_for(42.46139, -4.10713), ["catastro"])

    def test_catastro_is_always_the_last_resort(self):
        # Coverage gaps carry no fetcher and are never "tried", so the invariant
        # is over the providers that actually make a request.
        fetching = [p["name"] for p in parcel_proxy.PROVIDERS if p["fetch"] is not None]
        self.assertEqual(fetching[-1], "catastro")


class ValidationTests(unittest.TestCase):
    def test_non_numeric_input_is_rejected(self):
        result = parcel_proxy.get_parcels_near("abc", -4.1, 0.5)
        self.assertEqual(result["features"], [])
        self.assertIn("numeric", result["error"])

    def test_radius_bounds_are_enforced(self):
        for radius in (0.01, 99.0):
            result = parcel_proxy.get_parcels_near(42.46, -4.10, radius)
            self.assertIn("radius must be between", result["error"])

    def test_outside_spain_is_reported_not_fetched(self):
        result = parcel_proxy.get_parcels_near(39.0, -76.6, 0.5)  # Maryland
        self.assertEqual(result["error_code"], "outside_coverage")

    def test_gipuzkoa_is_a_known_gap_not_a_failure(self):
        # No provider bbox covers central Gipuzkoa, so nothing is fetched.
        with patch.object(parcel_proxy, "_SESSION") as session:
            session.get.return_value = Mock(
                json=Mock(return_value={"type": "FeatureCollection", "features": []}),
                content=b"<FeatureCollection/>",
                raise_for_status=Mock(),
            )
            result = parcel_proxy.get_parcels_near(43.3183, -1.9812, 0.4)
        self.assertEqual(result["error_code"], "no_open_service")
        self.assertIn("Gipuzkoa", result["error"])


class ProviderFetchTests(unittest.TestCase):
    def test_bizkaia_excludes_superseded_parcels(self):
        response = Mock()
        response.json.return_value = {
            "type": "FeatureCollection",
            "features": [{
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0]]]},
                "properties": {
                    "Codigo_Municipio": 20, "Codigo_Poligono": 1603,
                    "Codigo_Parcela": 2005, "Shape.STArea()": 823.4,
                },
            }],
        }
        with patch.object(parcel_proxy, "_SESSION") as session:
            session.get.return_value = response
            features = parcel_proxy._fetch_bizkaia(
                {"lat_min": 43.2, "lat_max": 43.3, "lon_min": -3.0, "lon_max": -2.9}
            )
            params = session.get.call_args.kwargs["params"]

        self.assertEqual(params["where"], "Es_Baja = 0")
        self.assertEqual(features[0]["properties"]["ref"], "20-1603-2005")
        self.assertEqual(features[0]["properties"]["area_m2"], 823.4)

    def test_navarra_unwraps_nested_json_properties(self):
        response = Mock()
        response.json.return_value = {
            "type": "FeatureCollection",
            "features": [{
                "geometry": {"type": "MultiPolygon", "coordinates": []},
                "properties": {
                    "areaValue": {"value": 619.06, "@uom": "m2"},
                    "inspireId": {"localId": "201010014", "namespace": "ES.RRTN.CP"},
                },
            }],
        }
        with patch.object(parcel_proxy, "_SESSION") as session:
            session.get.return_value = response
            features = parcel_proxy._fetch_navarra(
                {"lat_min": 42.8, "lat_max": 42.9, "lon_min": -1.7, "lon_max": -1.6}
            )

        self.assertEqual(features[0]["properties"]["area_m2"], 619.06)
        self.assertEqual(features[0]["properties"]["ref"], "201010014")

    def test_upstream_failure_falls_through_to_an_error(self):
        with patch.object(parcel_proxy, "_SESSION") as session:
            session.get.side_effect = OSError("connection reset")
            result = parcel_proxy.get_parcels_near(42.46139, -4.10713, 0.4)

        self.assertEqual(result["error_code"], "upstream_unavailable")

    def test_bbox_is_latitude_first_for_catastro(self):
        response = Mock()
        response.content = CATASTRO_GML
        with patch.object(parcel_proxy, "_SESSION") as session:
            session.get.return_value = response
            parcel_proxy._fetch_catastro(
                {"lat_min": 42.4, "lat_max": 42.5, "lon_min": -4.2, "lon_max": -4.0}
            )
            params = session.get.call_args.kwargs["params"]

        self.assertTrue(params["BBOX"].startswith("42.4,-4.2,42.5,-4.0"))
        self.assertIn("EPSG::4326", params["SRSNAME"])


if __name__ == "__main__":
    unittest.main()
