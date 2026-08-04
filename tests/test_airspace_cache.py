import unittest
from unittest.mock import Mock, patch

from chasemapper import airspace_cache


def polygon(west, south, east, north):
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def feature(geometry, **props):
    return {"type": "Feature", "geometry": geometry, "properties": props}


# Inside the northern-Spain region (Burgos province, near the launch site).
BURGOS = polygon(-3.9, 42.2, -3.6, 42.5)
# Outside it (Andalusia).
SEVILLE = polygon(-6.1, 37.2, -5.8, 37.5)


class RegionTests(unittest.TestCase):
    def test_region_covers_northern_spain_only(self):
        self.assertTrue(airspace_cache._geometry_intersects_region(BURGOS))
        self.assertFalse(airspace_cache._geometry_intersects_region(SEVILLE))

    def test_bbox_param_is_lon_lat_ordered(self):
        self.assertEqual(
            airspace_cache._bbox_geometry_param(), "-10.0,39.5,4.5,44.5"
        )


class VerticalLimitTests(unittest.TestCase):
    """ENAIRE's LOWER_VAL/UPPER_VAL are render sort keys, not altitudes: every
    ground-referenced value carries a flat +12200 ft offset. The limits must come
    from the DISTVERT* triplet instead."""

    def test_flight_level_converts_to_feet(self):
        feet, datum = airspace_cache._vertical_limit(
            {"DISTVERTUPPER_VAL": 145.0, "DISTVERTUPPER_UOM": "FL",
             "DISTVERTUPPER_CODE": "STD"}, "UPPER")
        self.assertEqual(feet, 14500.0)
        self.assertEqual(datum, "FL")

    def test_agl_limit_keeps_its_datum_and_ignores_sort_key(self):
        # Real CTR BILBAO values: 1000 ft AGL, but UPPER_VAL reports 13200.
        feet, datum = airspace_cache._vertical_limit(
            {"DISTVERTUPPER_VAL": 1000.0, "DISTVERTUPPER_UOM": "FT",
             "DISTVERTUPPER_CODE": "HEIG", "UPPER_VAL": 13200.0}, "UPPER")
        self.assertEqual(feet, 1000.0)
        self.assertEqual(datum, "AGL")

    def test_amsl_and_metres_are_converted(self):
        feet, datum = airspace_cache._vertical_limit(
            {"DISTVERTLOWER_VAL": 100.0, "DISTVERTLOWER_UOM": "M",
             "DISTVERTLOWER_CODE": "HEIS"}, "LOWER")
        self.assertAlmostEqual(feet, 328.084)
        self.assertEqual(datum, "AMSL")

    def test_missing_limit_is_none_not_zero(self):
        # Overflight-prohibited areas carry no ceiling at all.
        self.assertEqual(
            airspace_cache._vertical_limit(
                {"DISTVERTUPPER_VAL": None, "DISTVERTUPPER_UOM": None,
                 "UPPER_VAL": 99999.0}, "UPPER"),
            (None, ""),
        )


class NormalisationTests(unittest.TestCase):
    def test_html_entities_and_padding_are_cleaned(self):
        result = airspace_cache._normalise_feature(feature(
            BURGOS,
            TYPE_CODE="D", IDENT_TXT="LED10",
            NAME_TXT="CASTRILLO DEL VAL (Burgos)",
            NIVEL_INF="SFC", NIVEL_SUP="FL080",
            DISTVERTUPPER_VAL=80.0, DISTVERTUPPER_UOM="FL", DISTVERTUPPER_CODE="STD",
            REMARKS_TXT="Tiro terrestre.  Coordinaci&#243;n con BURGOS AFIS &amp; VITORIA TWR",
            WORKHR_CODE="NOTAM",
        ))
        props = result["properties"]
        self.assertEqual(props["name"], "CASTRILLO DEL VAL (Burgos)")
        self.assertEqual(props["ident"], "LED10")
        self.assertEqual(props["upper_ft"], 8000.0)
        self.assertEqual(props["schedule"], "Activated by NOTAM")
        self.assertEqual(
            props["remarks"],
            "Tiro terrestre. Coordinación con BURGOS AFIS & VITORIA TWR",
        )

    def test_name_falls_back_to_ident_then_type(self):
        self.assertEqual(
            airspace_cache._normalise_feature(
                feature(BURGOS, TYPE_CODE="R", IDENT_TXT="LER52")
            )["properties"]["name"],
            "LER52",
        )
        self.assertEqual(
            airspace_cache._normalise_feature(
                feature(BURGOS, TYPE_CODE="TSA")
            )["properties"]["name"],
            "TSA",
        )

    def test_schedule_combines_code_and_remark(self):
        self.assertEqual(
            airspace_cache._format_schedule({"WORKHR_CODE": "H24"}),
            "Continuous (H24)",
        )
        # RMK means "see the remark", so the remark stands alone.
        self.assertEqual(
            airspace_cache._format_schedule(
                {"WORKHR_CODE": "RMK", "WORKHRRMK_TXT": "MON/FRI EXC HOL"}
            ),
            "MON/FRI EXC HOL",
        )
        self.assertEqual(
            airspace_cache._format_schedule(
                {"WORKHR_CODE": "HJ", "WORKHRRMK_TXT": "Summer only"}
            ),
            "Sunrise to sunset (HJ) - Summer only",
        )

    def test_zone_flags_only_report_those_set(self):
        props = airspace_cache._normalise_feature(
            feature(BURGOS, TYPE_CODE="RMZ", RMZ="1", TMZ="0", FBZ="1")
        )["properties"]
        self.assertEqual(props["zones"], ["RMZ", "FBZ"])

    def test_frequency_gets_units_only_when_mhz(self):
        self.assertEqual(
            airspace_cache._normalise_feature(
                feature(BURGOS, FREQTRANS_VAL="118.375", FREQ_UOM="MHz")
            )["properties"]["frequency"],
            "118.375 MHz",
        )
        self.assertEqual(
            airspace_cache._normalise_feature(
                feature(BURGOS, FREQTRANS_VAL="118.375", FREQ_UOM="C")
            )["properties"]["frequency"],
            "118.375",
        )


class FetchTests(unittest.TestCase):
    def _response(self, features):
        response = Mock()
        response.json.return_value = {
            "type": "FeatureCollection", "features": features
        }
        return response

    @patch("chasemapper.airspace_cache.requests.get")
    def test_layer_query_filters_by_type_code(self, get):
        get.return_value = self._response([])

        airspace_cache._fetch_airspace("restricted")

        params = get.call_args.kwargs["params"]
        self.assertIn("TYPE_CODE IN (", params["where"])
        for code in ("'D'", "'R'", "'P'"):
            self.assertIn(code, params["where"])
        self.assertEqual(params["outSR"], "4326")
        self.assertEqual(params["f"], "geojson")
        get.return_value.raise_for_status.assert_called_once_with()

    @patch("chasemapper.airspace_cache.requests.get")
    def test_features_outside_region_and_without_geometry_are_dropped(self, get):
        get.return_value = self._response([
            feature(BURGOS, TYPE_CODE="D", NAME_TXT="keep"),
            feature(SEVILLE, TYPE_CODE="D", NAME_TXT="too far south"),
            feature(None, TYPE_CODE="D", NAME_TXT="no geometry"),
        ])

        result = airspace_cache._fetch_airspace("restricted")

        self.assertEqual(
            [f["properties"]["name"] for f in result["features"]], ["keep"]
        )

    @patch("chasemapper.airspace_cache.requests.get")
    def test_invalid_response_is_a_refresh_failure(self, get):
        response = Mock()
        response.json.return_value = []
        get.return_value = response

        with self.assertRaises(ValueError):
            airspace_cache._fetch_airspace("ctr")

    def test_every_layer_has_a_cache_path(self):
        # Layer types are the source of LAYERS, so only the paths can drift.
        for layer in airspace_cache.LAYERS:
            self.assertIn(layer, airspace_cache._LAYER_PATHS)

    def test_unknown_layer_is_rejected(self):
        with self.assertRaises(ValueError):
            airspace_cache._layer_paths("class_b")
        self.assertIsNone(airspace_cache.get_layer_path("tfr"))


if __name__ == "__main__":
    unittest.main()
