//
// CHASE - Browser-Based Chase Mapper - Map Pane Stack
//
// The one place the map's draw order is written down. Leaflet hit-tests the
// topmost DOM element under the pointer, so the paint order is also the click
// order: whatever sits highest here is what a click reaches first.
//
// Bottom to top:
//   395  kml-overlays  configured KML (the eclipse path) - backdrop only
//   400  overlayPane   Leaflet's own: balloon and chase-car tracks
//   410+ airspace-*    one pane per class, in the order recovery_overlays.js
//                      lists them
//   470  geofence      the flight's own boundary, above the airspace it spans
//   480  parcels       landing-site detail, the finest thing on the map
//   600  markerPane    Leaflet's own: landing markers and their Maps links
//
// Everything in these panes has to render as SVG. A Canvas renderer is a
// single viewport-sized element that hit-tests as opaque, so it would swallow
// every click meant for a pane below it whatever its z-index says.
//
//   Copyright (C) 2026 Huy Huong <huyhuong@umd.edu>
//   Released under GNU GPL v3 or later
//
(function () {
    "use strict";

    var KML_Z = 395;
    var AIRSPACE_Z = 410;       // first airspace class...
    var AIRSPACE_STEP = 5;      // ...and the gap up to each one above it
    var GEOFENCE_Z = 470;       // room for 12 airspace classes below here
    var PARCEL_Z = 480;

    function ensure(map, name, zIndex) {
        var pane = map.getPane(name) || map.createPane(name);
        pane.style.zIndex = String(zIndex);
        return name;
    }

    // Each accessor creates its pane on first use and returns the pane name,
    // so callers can write L.polygon(pts, {pane: MapPanes.geofence(map)})
    // without tracking whether it exists yet.
    window.MapPanes = {
        kml: function (map) {
            return ensure(map, "kml-overlays", KML_Z);
        },

        // One pane per airspace class rather than one for the lot: a click
        // inside a big transparent CTA has to fall through to the small
        // restricted area drawn on top of it.
        airspace: function (map, slug, index) {
            return ensure(map, "airspace-" + slug, AIRSPACE_Z + AIRSPACE_STEP * index);
        },

        geofence: function (map) {
            return ensure(map, "geofence", GEOFENCE_Z);
        },

        parcels: function (map) {
            return ensure(map, "parcels", PARCEL_Z);
        },

        // Geofence draw mode needs every click to reach the map so it can
        // become a vertex, but an interactive path consumes the click before
        // the map ever sees it. This class suspends hit-testing across the
        // whole vector stack for the duration of the draw - see
        // .map-draw-mode in chasemapper.css.
        setDrawMode: function (map, on) {
            map.getContainer().classList.toggle("map-draw-mode", !!on);
        }
    };
})();
