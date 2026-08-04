import json
import os
import tempfile
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


def collection(features, exceeded=False):
    """An ArcGIS geojson response. Note where the truncation flag lives: under
    f=geojson the server nests it in properties, not at the top level."""
    fc = {"type": "FeatureCollection", "features": features}
    if exceeded:
        fc["properties"] = {"exceededTransferLimit": True}
    return fc


class AirspaceCacheTests(unittest.TestCase):
    @patch("chasemapper.airspace_cache.requests.get")
    def test_class_e_query_includes_all_e_subtypes(self, get):
        response = Mock()
        response.json.return_value = collection([])
        get.return_value = response

        airspace_cache._fetch_class_airspace("class_e")

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["where"], "CLASS='E'")
        # Full-precision Class E geometry is tens of MB over the wire.
        self.assertEqual(params["geometryPrecision"], 5)
        response.raise_for_status.assert_called_once_with()

    @patch("chasemapper.airspace_cache.requests.get")
    def test_sua_query_shares_the_class_airspace_params(self, get):
        response = Mock()
        response.json.return_value = collection([])
        get.return_value = response

        airspace_cache._fetch_sua()

        self.assertEqual(get.call_args.args[0], airspace_cache._SUA_URL)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["where"], "1=1")
        self.assertEqual(params["geometryPrecision"], 5)
        self.assertEqual(params["f"], "geojson")
        self.assertEqual(params["outSR"], "4326")
        self.assertEqual(params["geometry"], airspace_cache._bbox_geometry_param())

    @patch("chasemapper.airspace_cache.requests.get")
    def test_truncated_arcgis_response_is_paged_to_completion(self, get):
        page_size = airspace_cache._ARCGIS_PAGE_SIZE
        pages = [
            collection([{"id": 1}], exceeded=True),
            collection([{"id": 2}], exceeded=True),
            collection([{"id": 3}]),
        ]
        get.side_effect = [Mock(**{"json.return_value": p}) for p in pages]

        result = airspace_cache._fetch_arcgis("http://x/query", "1=1")

        self.assertEqual(result["features"], [{"id": 1}, {"id": 2}, {"id": 3}])
        offsets = [c.kwargs["params"]["resultOffset"] for c in get.call_args_list]
        self.assertEqual(offsets, [0, page_size, 2 * page_size])
        # The merged collection must not inherit a page's truncation flag.
        self.assertNotIn("properties", result)

    @patch("chasemapper.airspace_cache.requests.get")
    def test_endless_truncation_raises_rather_than_caching_a_partial_layer(self, get):
        get.return_value = Mock(
            **{"json.return_value": collection([{"id": 1}], exceeded=True)}
        )

        with self.assertRaises(ValueError):
            airspace_cache._fetch_arcgis("http://x/query", "1=1")

        self.assertEqual(get.call_count, airspace_cache._ARCGIS_MAX_PAGES)

    @patch("chasemapper.airspace_cache.requests.get")
    def test_null_features_is_rejected_at_the_fetch_boundary(self, get):
        get.return_value = Mock(
            **{"json.return_value": {"type": "FeatureCollection", "features": None}}
        )

        with self.assertRaises(ValueError):
            airspace_cache._fetch_arcgis("http://x/query", "1=1")

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


def fc(features, **extra):
    collection = {"type": "FeatureCollection", "features": features}
    collection.update(extra)
    return collection


class LayerWriteTests(unittest.TestCase):
    """A layer and its provenance are published as one file, or not at all."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.geo_path = os.path.join(self.tmpdir.name, "class_e.geojson")
        patcher = patch.dict(
            airspace_cache._LAYER_PATHS, {"class_e": self.geo_path}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        meta = patch.dict(airspace_cache._meta_cache, {}, clear=True)
        meta.start()
        self.addCleanup(meta.stop)

    def read(self):
        with open(self.geo_path) as f:
            return json.load(f)

    def leftover_temps(self):
        return [n for n in os.listdir(self.tmpdir.name) if n.endswith(".tmp")]

    def test_geojson_carries_its_own_provenance(self):
        airspace_cache._write_layer("class_e", fc([{"id": 1}]), 1000.0)

        written = self.read()
        self.assertEqual(written["features"], [{"id": 1}])
        self.assertEqual(written["fetched_at"], 1000.0)
        self.assertEqual(written["feature_count"], 1)
        # Still a valid FeatureCollection — the extras are GeoJSON foreign members.
        self.assertEqual(written["type"], "FeatureCollection")

    def test_write_does_not_mutate_the_callers_collection(self):
        payload = fc([{"id": 1}])
        airspace_cache._write_layer("class_e", payload, 1000.0)
        self.assertEqual(payload, {"type": "FeatureCollection", "features": [{"id": 1}]})

    def test_a_failed_replace_leaves_the_previous_cache_wholly_intact(self):
        """The promote step is the one that can destroy a good cache."""
        airspace_cache._write_layer("class_e", fc([{"id": "old"}]), 1.0)

        with patch.object(
            airspace_cache.os, "replace", side_effect=OSError("EIO")
        ):
            with self.assertRaises(OSError):
                airspace_cache._write_layer("class_e", fc([{"id": "new"}]), 2.0)

        # _try_refresh logs "keeping stale cache" on this path, so the stale
        # cache must still be there — geometry and provenance together.
        survivor = self.read()
        self.assertEqual(survivor["features"], [{"id": "old"}])
        self.assertEqual(survivor["fetched_at"], 1.0)
        self.assertEqual(survivor["feature_count"], 1)
        self.assertEqual(self.leftover_temps(), [])
        # get_status must not advertise the fetch that never landed.
        self.assertEqual(airspace_cache._layer_meta("class_e")["fetched_at"], 1.0)

    def test_a_failed_stage_leaves_the_previous_cache_intact(self):
        airspace_cache._write_layer("class_e", fc([{"id": "old"}]), 1.0)

        with patch.object(
            airspace_cache, "_stage_json", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaises(OSError):
                airspace_cache._write_layer("class_e", fc([{"id": "new"}]), 2.0)

        self.assertEqual(self.read()["features"], [{"id": "old"}])
        self.assertEqual(self.leftover_temps(), [])

    def test_readers_never_see_geometry_without_matching_provenance(self):
        """Whatever a reader observes, the two halves agree — there is no
        window in which new features are described by an older fetch."""
        airspace_cache._write_layer("class_e", fc([{"id": "old"}]), 1.0)
        seen = []
        real_replace = os.replace

        def observe(src, dst):
            seen.append(airspace_cache._read_layer("class_e"))
            real_replace(src, dst)
            seen.append(airspace_cache._read_layer("class_e"))

        with patch.object(airspace_cache.os, "replace", side_effect=observe):
            airspace_cache._write_layer("class_e", fc([{"id": "new"}, {"id": 2}]), 2.0)

        for observed in seen:
            self.assertEqual(observed["feature_count"], len(observed["features"]))
            expected = 1.0 if observed["features"] == [{"id": "old"}] else 2.0
            self.assertEqual(observed["fetched_at"], expected)

    def test_staging_uses_unique_temp_names(self):
        """Two refresh rounds can overlap on one layer; a shared '<path>.tmp'
        would let one promote a file the other is still writing."""
        first = airspace_cache._stage_json(self.geo_path, {"a": 1})
        second = airspace_cache._stage_json(self.geo_path, {"a": 2})
        self.addCleanup(airspace_cache._discard, first)
        self.addCleanup(airspace_cache._discard, second)

        self.assertNotEqual(first, second)
        with open(first) as f:
            self.assertEqual(json.load(f), {"a": 1})
        with open(second) as f:
            self.assertEqual(json.load(f), {"a": 2})


if __name__ == "__main__":
    unittest.main()
