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


class AirspaceCacheTests(unittest.TestCase):
    @patch("chasemapper.airspace_cache.requests.get")
    def test_class_e_query_includes_all_e_subtypes(self, get):
        response = Mock()
        response.json.return_value = {"type": "FeatureCollection", "features": []}
        get.return_value = response

        airspace_cache._fetch_class_airspace("class_e")

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["where"], "CLASS='E'")
        response.raise_for_status.assert_called_once_with()

    def test_geometry_bounds_detect_crossing_polygon(self):
        crossing = polygon(-84.0, 38.0, -74.0, 39.0)
        outside = polygon(-100.0, 30.0, -99.0, 31.0)

        self.assertTrue(airspace_cache._geometry_intersects_region(crossing))
        self.assertFalse(airspace_cache._geometry_intersects_region(outside))

    @patch("chasemapper.airspace_cache.requests.get")
    def test_tfr_wfs_features_are_filtered_and_normalized(self, get):
        response = Mock()
        response.json.return_value = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": polygon(-79.5, 39.0, -79.0, 39.5),
                    "properties": {
                        "NOTAM_KEY": "6/1234-1-FDC-F",
                        "TITLE": "Example restriction",
                        "LEGAL": "HAZARDS",
                        "LAST_MODIFICATION_DATETIME": "202607170900",
                    },
                },
                {
                    "type": "Feature",
                    "geometry": polygon(-120.0, 35.0, -119.0, 36.0),
                    "properties": {"NOTAM_KEY": "6/9999-1-FDC-F"},
                },
            ],
        }
        get.return_value = response

        result = airspace_cache._fetch_tfrs()

        self.assertEqual(len(result["features"]), 1)
        props = result["features"][0]["properties"]
        self.assertEqual(props["notam_id"], "6/1234")
        self.assertEqual(props["type"], "HAZARDS")
        self.assertEqual(props["description"], "Example restriction")
        self.assertEqual(props["last_modified"], "202607170900")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["typeName"], "TFR:V_TFR_LOC")
        self.assertEqual(params["srsname"], "EPSG:4326")

    @patch("chasemapper.airspace_cache.requests.get")
    def test_invalid_tfr_response_is_a_refresh_failure(self, get):
        response = Mock()
        response.json.return_value = []
        get.return_value = response

        with self.assertRaises(ValueError):
            airspace_cache._fetch_tfrs()


if __name__ == "__main__":
    unittest.main()
