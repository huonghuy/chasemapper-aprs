import os
import tempfile
import unittest
from xml.etree import ElementTree as ET

from chasemapper import kml_export


KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


def telemetry(callsign, lat, lon, alt, time="2026-08-12T17:00:00+00:00"):
    return {
        "log_type": "BALLOON TELEMETRY",
        "log_time": time,
        "time": time,
        "callsign": callsign,
        "lat": lat,
        "lon": lon,
        "alt": alt,
    }


def prediction(callsign, path, landing=None, burst=None, time="2026-08-12T17:01:00+00:00"):
    return {
        "log_type": "PREDICTION",
        "log_time": time,
        "callsign": callsign,
        "pred_path": path,
        "pred_landing": landing if landing is not None else path[-1],
        "burst": burst if burst is not None else [],
        "abort_path": [],
        "abort_landing": [],
    }


# A short ascent, then the launch prediction, then a later (converged)
# prediction that the export should ignore.
ASCENT = [
    telemetry("KC1RBW-11", 42.4614, -4.1071, 800.0, "2026-08-12T17:00:00+00:00"),
    telemetry("KC1RBW-11", 42.4700, -4.0900, 5000.0, "2026-08-12T17:10:00+00:00"),
    telemetry("KC1RBW-11", 42.4900, -4.0500, 24000.0, "2026-08-12T17:30:00+00:00"),
    telemetry("KC1RBW-11", 42.5100, -3.9800, 900.0, "2026-08-12T17:50:00+00:00"),
]

LAUNCH_PRED = prediction(
    "KC1RBW-11",
    [[42.4614, -4.1071, 800.0], [42.48, -4.05, 20000.0], [42.55, -3.90, 850.0]],
    landing=[42.55, -3.90, 850.0],
    burst=[42.48, -4.05, 20000.0],
    time="2026-08-12T17:02:00+00:00",
)

LATER_PRED = prediction(
    "KC1RBW-11",
    [[42.49, -4.05, 24000.0], [42.60, -3.70, 900.0]],
    landing=[42.60, -3.70, 900.0],
    time="2026-08-12T17:31:00+00:00",
)

GEOFENCE = {
    "polygon": [[42.3, -4.4], [42.3, -3.7], [42.7, -3.7], [42.7, -4.4]],
    "min_alt": -500.0,
    "max_alt": 23000.0,
    "remain": "inside",
}

OVERLAY_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>Eclipse</name>
  <description>Should not leak into the folder name.</description>
  <Placemark>
    <name>Central line</name>
    <LineString><coordinates>-4.1,42.4,0 -3.1,41.8,0</coordinates></LineString>
  </Placemark>
  <Placemark>
    <name>Sordillos, Spain</name>
    <Point><coordinates>-4.107130,42.461390,0</coordinates></Point>
  </Placemark>
</Document>
</kml>
"""


def parse(kml):
    return ET.fromstring(kml)


def folder_names(root):
    return [
        el.findtext("kml:name", "", KML_NS)
        for el in root.iter("{http://www.opengis.net/kml/2.2}Folder")
    ]


def placemark_names(root):
    return [
        el.findtext("kml:name", "", KML_NS)
        for el in root.iter("{http://www.opengis.net/kml/2.2}Placemark")
    ]


class CollectFlightDataTests(unittest.TestCase):
    def test_keeps_only_the_first_prediction(self):
        flights = kml_export.collect_flight_data(
            ASCENT + [LAUNCH_PRED, LATER_PRED]
        )
        pred = flights["KC1RBW-11"]["prediction"]
        self.assertEqual(pred["time"], "2026-08-12T17:02:00+00:00")
        self.assertEqual(len(pred["path"]), 3)

    def test_track_stays_in_log_order(self):
        flights = kml_export.collect_flight_data(ASCENT)
        alts = [p[3] for p in flights["KC1RBW-11"]["track"]]
        self.assertEqual(alts, [800.0, 5000.0, 24000.0, 900.0])

    def test_separates_payloads_and_can_filter(self):
        entries = ASCENT + [telemetry("W3EAX-9", 42.0, -4.0, 100.0)]
        self.assertEqual(
            sorted(kml_export.collect_flight_data(entries)),
            ["KC1RBW-11", "W3EAX-9"],
        )
        self.assertEqual(
            sorted(kml_export.collect_flight_data(entries, callsigns=["W3EAX-9"])),
            ["W3EAX-9"],
        )

    def test_callsign_filter_is_case_insensitive(self):
        # read_config upper-cases the profile callsigns; a telemetry
        # source need not.
        entries = [telemetry("kc1mhu-11", 42.0, -4.0, 100.0)] * 2
        self.assertEqual(
            sorted(kml_export.collect_flight_data(entries, callsigns=["KC1MHU-11"])),
            ["kc1mhu-11"],
        )

    def test_callsign_filter_excludes_other_balloons(self):
        entries = (
            ASCENT
            + [telemetry("KC1MHU-11", 42.0, -4.0, 100.0)] * 2
            + [telemetry("KC1MHU-12", 42.1, -4.1, 200.0)] * 2
        )
        self.assertEqual(
            sorted(kml_export.collect_flight_data(entries, callsigns=["KC1MHU-11"])),
            ["KC1MHU-11"],
        )

    def test_ignores_chase_car_positions(self):
        car = {
            "log_type": "CAR POSITION",
            "log_time": "2026-08-12T17:00:00+00:00",
            "time": "2026-08-12T17:00:00+00:00",
            "lat": 42.4,
            "lon": -4.1,
            "alt": 900.0,
            "comment": "KC1RBW-2",
        }
        flights = kml_export.collect_flight_data(ASCENT + [car])
        self.assertEqual(sorted(flights), ["KC1RBW-11"])
        self.assertEqual(len(flights["KC1RBW-11"]["track"]), len(ASCENT))

    def test_ignores_entries_without_a_usable_position(self):
        bad = dict(telemetry("KC1RBW-11", 0, 0, 0))
        bad["lat"] = None
        flights = kml_export.collect_flight_data([bad])
        self.assertEqual(flights, {})

    def test_drops_payloads_with_a_prediction_but_no_track(self):
        self.assertEqual(kml_export.collect_flight_data([LAUNCH_PRED]), {})

    def test_ignores_a_prediction_with_a_degenerate_path(self):
        stub = prediction("KC1RBW-11", [[42.4, -4.1, 800.0]])
        flights = kml_export.collect_flight_data(ASCENT + [stub])
        self.assertIsNone(flights["KC1RBW-11"]["prediction"])


class SummarisePayloadsTests(unittest.TestCase):
    def test_reports_stats_per_payload(self):
        entries = (
            ASCENT
            + [LAUNCH_PRED]
            + [telemetry("KC1MHU-12", 42.1, -4.1, 200.0)] * 3
        )
        payloads = {p["callsign"]: p for p in kml_export.summarise_payloads(entries)}

        self.assertEqual(sorted(payloads), ["KC1MHU-12", "KC1RBW-11"])
        self.assertEqual(payloads["KC1RBW-11"]["points"], 4)
        self.assertEqual(payloads["KC1RBW-11"]["max_alt"], 24000.0)
        self.assertTrue(payloads["KC1RBW-11"]["prediction"])
        self.assertEqual(payloads["KC1MHU-12"]["points"], 3)
        self.assertFalse(payloads["KC1MHU-12"]["prediction"])

    def test_empty_log(self):
        self.assertEqual(kml_export.summarise_payloads([]), [])


class BuildFlightKmlTests(unittest.TestCase):
    def build(self, **kwargs):
        kwargs.setdefault("log_entries", ASCENT + [LAUNCH_PRED, LATER_PRED])
        return kml_export.build_flight_kml(**kwargs)

    def test_output_is_well_formed_and_has_every_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "eclipse.kml")
            with open(path, "wb") as f:
                f.write(OVERLAY_KML)

            kml, summary = self.build(
                geofence=GEOFENCE,
                profile_name="Command",
                overlays=[{"name": "Eclipse Path", "path": path, "visible": True}],
                log_name="20260812-1700Z.log",
            )

        root = parse(kml)
        names = folder_names(root)
        self.assertIn("Flight - KC1RBW-11", names)
        self.assertIn("Actual path (APRS)", names)
        self.assertIn("Launch prediction", names)
        self.assertIn("Geofence - Command", names)
        self.assertIn("Eclipse Path", names)

        marks = placemark_names(root)
        self.assertIn("Launch", marks)
        self.assertIn("Max altitude", marks)
        self.assertIn("Last packet", marks)
        self.assertIn("Predicted landing", marks)
        # Overlay content carried across intact.
        self.assertIn("Sordillos, Spain", marks)

        self.assertEqual(summary["callsigns"], ["KC1RBW-11"])
        self.assertEqual(summary["points"], 4)
        self.assertEqual(summary["predictions"], 1)
        self.assertTrue(summary["geofence"])
        self.assertEqual(summary["overlays"], 1)

    def test_paths_use_absolute_altitude(self):
        kml, _ = self.build()
        root = parse(kml)
        for line in root.iter("{http://www.opengis.net/kml/2.2}LineString"):
            self.assertEqual(line.findtext("kml:altitudeMode", "", KML_NS), "absolute")

    def test_predicted_path_coordinates_are_lon_lat_alt(self):
        kml, _ = self.build()
        root = parse(kml)
        for placemark in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
            if placemark.findtext("kml:name", "", KML_NS) != "Predicted path":
                continue
            coords = placemark.find(".//kml:coordinates", KML_NS).text.split()
            self.assertEqual(coords[0], "-4.107100,42.461400,800.0")
            return
        self.fail("No 'Predicted path' placemark in the export.")

    def test_geofence_ring_is_closed(self):
        kml, _ = self.build(geofence=GEOFENCE, profile_name="Command")
        root = parse(kml)
        ring = root.find(".//kml:LinearRing/kml:coordinates", KML_NS).text.split()
        self.assertEqual(len(ring), len(GEOFENCE["polygon"]) + 1)
        self.assertEqual(ring[0], ring[-1])

    def test_remain_outside_geofence_uses_its_own_style(self):
        fence = dict(GEOFENCE, remain="outside")
        kml, _ = self.build(geofence=fence, profile_name="Command")
        self.assertIn("<styleUrl>#fenceOutside</styleUrl>", kml)
        self.assertNotIn("<styleUrl>#fenceInside</styleUrl>", kml)

    def test_no_geofence_folder_when_none_set(self):
        kml, summary = self.build(geofence=None)
        self.assertFalse(summary["geofence"])
        self.assertNotIn("Geofence", folder_names(parse(kml)))

    def test_missing_overlay_file_is_skipped_not_fatal(self):
        kml, summary = self.build(
            overlays=[{"name": "Gone", "path": "/nonexistent/eclipse.kml"}]
        )
        self.assertEqual(summary["overlays"], 0)
        parse(kml)  # still well-formed

    def test_empty_log_produces_no_callsigns(self):
        kml, summary = kml_export.build_flight_kml([])
        self.assertEqual(summary["callsigns"], [])
        self.assertEqual(summary["points"], 0)
        parse(kml)

    def test_text_is_escaped(self):
        entries = [telemetry("A&B<C>", 42.0, -4.0, 100.0)] * 2
        kml, _ = kml_export.build_flight_kml(entries)
        self.assertIn("Flight - A&amp;B&lt;C&gt;", kml)
        # ...and survives a round trip through the parser unmangled.
        self.assertIn("Flight - A&B<C>", folder_names(parse(kml)))

    def test_max_altitude_pin_omitted_for_a_ground_only_log(self):
        flat = [
            telemetry("KC1RBW-11", 42.46, -4.10, 800.0),
            telemetry("KC1RBW-11", 42.46, -4.10, 805.0),
        ]
        kml, _ = kml_export.build_flight_kml(flat)
        self.assertNotIn("Max altitude", placemark_names(parse(kml)))


if __name__ == "__main__":
    unittest.main()
