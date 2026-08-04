//
// CHASE - Browser-Based Chase Mapper - Geofence Overlay
//
// Renders a per-profile choppies geofence polygon on the Leaflet map
// and lets the operator manage it from the sidebar. The polygon can
// originate from a HAB Bounder KML upload OR from clicking out the
// vertices directly on the map — both paths produce the same record
// in geofences.json and render identically.
//
// Backend contract:
//   - GET    /geofence/<profile_id>          -> {geofence, has_trash}
//   - POST   /geofence/<profile_id>          multipart 'kml' / raw KML
//                                             OR JSON {polygon,min_alt,max_alt,remain}
//   - DELETE /geofence/<profile_id>          (soft-delete — pushes to trash)
//   - POST   /geofence/<profile_id>/restore  (pop most recent from trash)
//   - SocketIO 'geofence_update' {profile, geofence, has_trash} on every mutation
//   - chase_config.profiles[<id>].geofence + .geofence_has_trash populated by
//     /get_config and server_settings_update so reloads / late connects
//     render correctly and the Restore button starts in the right state.
//
// Drawing:
//   - Saved polygons: green for "remain inside", red for "remain outside".
//   - Draw mode: each click on the map drops a draggable vertex marker
//     (3..7 vertices). A dashed preview polyline tracks the in-progress
//     shape. "Finish" POSTs as JSON; "Cancel" discards.
//   - Restore: undoes the most recent clear OR overwrite. The currently
//     active geofence (if any) is itself pushed to trash, so Restore
//     is itself undoable by hitting Restore again.
//
//   Copyright (C) 2026 Huy Huong <huyhuong@umd.edu>
//   Released under GNU GPL v3 or later
//
(function () {
    "use strict";

    var STYLE = {
        inside:  { color: "#16a34a", weight: 2, fillColor: "#16a34a", fillOpacity: 0.10 },
        outside: { color: "#dc2626", weight: 2, fillColor: "#dc2626", fillOpacity: 0.15 }
    };

    // In-progress draw uses a distinct yellow so it doesn't read as a
    // committed geofence.
    var DRAW_PREVIEW_STYLE = {
        color: "#f59e0b", weight: 2, dashArray: "6 4",
        fillColor: "#f59e0b", fillOpacity: 0.05
    };
    var DRAW_VERTEX_RADIUS = 6;
    var MIN_VERTICES = 3;
    var MAX_VERTICES = 7;

    var state = {
        map: null,
        layer: null,        // current L.polygon for saved geofence, or null
        currentProfile: "", // profile name the drawn polygon belongs to
        cache: {},          // {profileName: geofenceDict | null} mirrored from server
        trashCache: {},     // {profileName: bool} - has at least one trash entry
        visible: true,      // user-controlled toggle (independent of cache)
        // Draw-mode state:
        drawing: false,
        drawMarkers: [],    // L.circleMarker per vertex
        drawPreview: null,  // L.polyline showing the in-progress ring
        savedCursor: null,  // map container cursor we replaced while drawing
        clickHandler: null
    };

    function $(id) { return document.getElementById(id); }

    function setStatus(text, isWarning) {
        var el = $("geofence-status");
        if (!el) return;
        el.textContent = text || "";
        el.style.color = isWarning ? "#dc2626" : "#6b7280";
    }

    function activeProfile() {
        // chase_config is the global populated by settings.js
        if (typeof chase_config !== "undefined" && chase_config) {
            return chase_config.selected_profile || "";
        }
        return "";
    }

    function clearLayer() {
        if (state.layer && state.map) {
            state.map.removeLayer(state.layer);
        }
        state.layer = null;
    }

    function popupHtml(profile, gf) {
        var altLine =
            "Min Alt: " + gf.min_alt + " m<br>" +
            "Max Alt: " + gf.max_alt + " m";
        return (
            "<b>Geofence — " + profile + "</b><br>" +
            "Remain <b>" + gf.remain + "</b><br>" +
            altLine + "<br>" +
            "<small>" + gf.polygon.length + " vertices</small>"
        );
    }

    function drawForProfile(profile) {
        clearLayer();
        state.currentProfile = profile;
        var gf = state.cache[profile];
        if (!gf || !gf.polygon || gf.polygon.length < 3) {
            setStatus(profile ? "No geofence set for this profile." : "");
            updateButtons(false);
            // Reflect cleared state in the input controls.
            syncInputsFromGeofence(null);
            return;
        }
        updateButtons(true);
        syncInputsFromGeofence(gf);
        if (!state.visible) {
            // Cached but hidden by user toggle.
            setStatus(
                "Geofence hidden — remain " + gf.remain +
                " (" + gf.polygon.length + " pts, " +
                gf.min_alt + "–" + gf.max_alt + " m)"
            );
            return;
        }
        var style = STYLE[gf.remain] || STYLE.inside;
        state.layer = L.polygon(gf.polygon, style)
            .bindPopup(popupHtml(profile, gf))
            .addTo(state.map);
        setStatus(
            "Geofence: remain " + gf.remain +
            " (" + gf.polygon.length + " pts, " +
            gf.min_alt + "–" + gf.max_alt + " m)"
        );
    }

    function updateButtons(haveFence) {
        var clr = $("geofence-clear");
        if (clr) clr.disabled = !haveFence || state.drawing;
        var copy = $("geofence-copy");
        if (copy) copy.disabled = !haveFence;
        var draw = $("geofence-draw");
        if (draw) draw.disabled = state.drawing;
        var restore = $("geofence-restore");
        if (restore) {
            restore.disabled = state.drawing || !state.trashCache[activeProfile()];
        }
        var finish = $("geofence-finish");
        var cancel = $("geofence-cancel-draw");
        if (finish) finish.disabled = !state.drawing || state.drawMarkers.length < MIN_VERTICES;
        if (cancel) cancel.disabled = !state.drawing;
    }

    function syncInputsFromGeofence(gf) {
        // Mirror the server's record into the sidebar inputs whenever
        // the saved geofence changes — that way the user sees what is
        // actually set, and editing inputs only matters for new draws.
        var minEl = $("geofence-min-alt");
        var maxEl = $("geofence-max-alt");
        if (gf) {
            if (minEl) minEl.value = gf.min_alt;
            if (maxEl) maxEl.value = gf.max_alt;
            var radios = document.getElementsByName("geofence-remain");
            for (var i = 0; i < radios.length; i++) {
                radios[i].checked = (radios[i].value === gf.remain);
            }
        }
        // When cleared we deliberately leave the inputs alone so the
        // user can keep their preferred defaults for the next draw.
    }

    function readInputs() {
        var minEl = $("geofence-min-alt");
        var maxEl = $("geofence-max-alt");
        var min_alt = minEl ? parseFloat(minEl.value) : NaN;
        var max_alt = maxEl ? parseFloat(maxEl.value) : NaN;
        var remain = "inside";
        var radios = document.getElementsByName("geofence-remain");
        for (var i = 0; i < radios.length; i++) {
            if (radios[i].checked) { remain = radios[i].value; break; }
        }
        return { min_alt: min_alt, max_alt: max_alt, remain: remain };
    }

    // Pull geofences out of a server config blob into our local cache.
    function syncCacheFromConfig(cfg) {
        if (!cfg || !cfg.profiles) return;
        Object.keys(cfg.profiles).forEach(function (name) {
            state.cache[name] = cfg.profiles[name].geofence || null;
            state.trashCache[name] = !!cfg.profiles[name].geofence_has_trash;
        });
    }

    // Every geofence mutation has the same shape: send, parse the body whether
    // or not the status was ok, adopt the server's new record. The originating
    // client applies it locally rather than waiting for the server's
    // geofence_update broadcast to come back around.
    function mutateGeofence(profile, url, opts, verb, onSuccess) {
        fetch(url, opts)
            .then(function (r) {
                return r.json().then(function (j) { return { ok: r.ok, body: j }; });
            })
            .then(function (res) {
                if (!res.ok) {
                    setStatus(verb + " failed: " + (res.body.error || "unknown"), true);
                    return;
                }
                if (onSuccess) onSuccess();
                state.cache[profile] = res.body.geofence || null;
                state.trashCache[profile] = !!res.body.has_trash;
                if (profile === activeProfile()) drawForProfile(profile);
            })
            .catch(function (e) {
                setStatus(verb + " error: " + e.message, true);
            });
    }

    function geofenceUrl(profile) {
        return "/geofence/" + encodeURIComponent(profile);
    }

    function uploadFile(file) {
        var profile = activeProfile();
        if (!profile) {
            setStatus("No profile selected.", true);
            return;
        }
        if (!file) return;
        var fd = new FormData();
        fd.append("kml", file);
        setStatus("Uploading " + file.name + "…");
        mutateGeofence(profile, geofenceUrl(profile), { method: "POST", body: fd }, "Upload");
    }

    function clearActive() {
        var profile = activeProfile();
        if (!profile) return;
        if (!confirm("Clear geofence for profile '" + profile + "'? You'll be able to Restore it for 2 days.")) return;
        mutateGeofence(profile, geofenceUrl(profile), { method: "DELETE" }, "Clear");
    }

    function restoreActive() {
        var profile = activeProfile();
        if (!profile) return;
        mutateGeofence(profile, geofenceUrl(profile) + "/restore", { method: "POST" }, "Restore");
    }

    // Emits the chase-team clipboard format:
    //     Remain: inside
    //     Altitude: -500, 23000
    //     Waypoint: lat,lon
    function copyWaypoints() {
        var profile = activeProfile();
        var gf = state.cache[profile];
        if (!gf || !gf.polygon || gf.polygon.length < 3) {
            setStatus("No geofence to copy.", true);
            return;
        }
        var lines = [
            "Remain: " + gf.remain,
            "Altitude: " + gf.min_alt + ", " + gf.max_alt
        ];
        for (var i = 0; i < gf.polygon.length; i++) {
            var pt = gf.polygon[i];
            lines.push("Waypoint: " + pt[0] + "," + pt[1]);
        }
        if (typeof textToClipboard === "function") {
            textToClipboard(lines.join("\n"));
            setStatus("Copied " + gf.polygon.length + " waypoints to clipboard.");
        } else {
            setStatus("Clipboard helper unavailable.", true);
        }
    }

    function rebuildPreview() {
        var latlngs = state.drawMarkers.map(function (m) { return m.getLatLng(); });
        if (!state.drawPreview) {
            // <2 vertices: nothing to draw yet.
            if (latlngs.length < 2) return;
            state.drawPreview = L.polyline(latlngs, DRAW_PREVIEW_STYLE).addTo(state.map);
        } else {
            state.drawPreview.setLatLngs(latlngs);
        }
    }

    function refreshDrawStatus() {
        var n = state.drawMarkers.length;
        if (n === 0) {
            setStatus("Drawing: click on the map to place the first vertex (3–7 total). Drag pins to adjust.");
        } else if (n < MIN_VERTICES) {
            setStatus("Drawing: " + n + "/" + MIN_VERTICES + " minimum vertices. Keep clicking.");
        } else if (n < MAX_VERTICES) {
            setStatus("Drawing: " + n + " vertices. Click to add more or hit Finish.");
        } else {
            setStatus("Drawing: " + MAX_VERTICES + " vertex maximum reached. Hit Finish.");
        }
        updateButtons(!!state.cache[activeProfile()]);
    }

    function addDrawVertex(latlng) {
        if (state.drawMarkers.length >= MAX_VERTICES) return;
        // L.circleMarker is the natural fit visually but doesn't support
        // dragging, so we use L.marker with a DivIcon shaped like one.
        var icon = L.divIcon({
            className: "geofence-draw-vertex",
            iconSize: [DRAW_VERTEX_RADIUS * 2, DRAW_VERTEX_RADIUS * 2],
            iconAnchor: [DRAW_VERTEX_RADIUS, DRAW_VERTEX_RADIUS],
            html: '<div style="width:' + (DRAW_VERTEX_RADIUS * 2) + 'px;height:' +
                  (DRAW_VERTEX_RADIUS * 2) + 'px;border-radius:50%;background:#fff;' +
                  'border:2px solid #f59e0b;box-shadow:0 0 0 1px rgba(0,0,0,0.2);"></div>'
        });
        var dm = L.marker(latlng, { icon: icon, draggable: true }).addTo(state.map);
        dm.on("drag", function () { rebuildPreview(); });
        // Right-click a vertex during draw to remove it (handy if you
        // misclick). Left-clicks still place new vertices.
        dm.on("contextmenu", function (e) {
            L.DomEvent.stopPropagation(e);
            removeDrawVertex(dm);
        });
        state.drawMarkers.push(dm);
        rebuildPreview();
        refreshDrawStatus();
    }

    function removeDrawVertex(marker) {
        var idx = state.drawMarkers.indexOf(marker);
        if (idx < 0) return;
        state.map.removeLayer(marker);
        state.drawMarkers.splice(idx, 1);
        rebuildPreview();
        refreshDrawStatus();
    }

    function startDraw() {
        if (state.drawing) return;
        var profile = activeProfile();
        if (!profile) {
            setStatus("Pick a profile first.", true);
            return;
        }
        state.drawing = true;
        // Hide any committed polygon so the user isn't editing on top
        // of stale geometry; it'll come back if they hit Cancel.
        clearLayer();
        state.drawMarkers = [];
        state.drawPreview = null;

        var container = state.map.getContainer();
        state.savedCursor = container.style.cursor;
        container.style.cursor = "crosshair";

        state.clickHandler = function (e) { addDrawVertex(e.latlng); };
        state.map.on("click", state.clickHandler);
        refreshDrawStatus();
    }

    function teardownDrawMode() {
        if (!state.drawing) return;
        if (state.clickHandler) {
            state.map.off("click", state.clickHandler);
            state.clickHandler = null;
        }
        for (var i = 0; i < state.drawMarkers.length; i++) {
            state.map.removeLayer(state.drawMarkers[i]);
        }
        state.drawMarkers = [];
        if (state.drawPreview) {
            state.map.removeLayer(state.drawPreview);
            state.drawPreview = null;
        }
        var container = state.map.getContainer();
        container.style.cursor = state.savedCursor || "";
        state.savedCursor = null;
        state.drawing = false;
    }

    function cancelDraw() {
        if (!state.drawing) return;
        teardownDrawMode();
        // Restore the previously-committed polygon for the active
        // profile (drawForProfile is a no-op safe redraw).
        drawForProfile(activeProfile());
        setStatus("Drawing cancelled.");
    }

    function finishDraw() {
        if (!state.drawing) return;
        if (state.drawMarkers.length < MIN_VERTICES) {
            setStatus("Need at least " + MIN_VERTICES + " vertices.", true);
            return;
        }
        var profile = activeProfile();
        var inputs = readInputs();
        if (!isFinite(inputs.min_alt) || !isFinite(inputs.max_alt)) {
            setStatus("Min/Max altitude must be numeric.", true);
            return;
        }
        if (inputs.min_alt >= inputs.max_alt) {
            setStatus("Min altitude must be less than max altitude.", true);
            return;
        }
        var polygon = state.drawMarkers.map(function (m) {
            var ll = m.getLatLng();
            return [ll.lat, ll.lng];
        });
        var body = {
            polygon: polygon,
            min_alt: inputs.min_alt,
            max_alt: inputs.max_alt,
            remain: inputs.remain
        };
        setStatus("Saving drawn geofence (" + polygon.length + " vertices)…");
        mutateGeofence(profile, geofenceUrl(profile), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        }, "Save", teardownDrawMode);
    }

    function wireUI() {
        var fileInput = $("geofence-file");
        if (fileInput) {
            fileInput.addEventListener("change", function () {
                if (fileInput.files && fileInput.files[0]) {
                    uploadFile(fileInput.files[0]);
                    fileInput.value = ""; // allow re-uploading same filename
                }
            });
        }
        var clr = $("geofence-clear");
        if (clr) clr.addEventListener("click", clearActive);

        var restore = $("geofence-restore");
        if (restore) restore.addEventListener("click", restoreActive);

        var copy = $("geofence-copy");
        if (copy) copy.addEventListener("click", copyWaypoints);

        var draw = $("geofence-draw");
        if (draw) draw.addEventListener("click", startDraw);
        var finish = $("geofence-finish");
        if (finish) finish.addEventListener("click", finishDraw);
        var cancel = $("geofence-cancel-draw");
        if (cancel) cancel.addEventListener("click", cancelDraw);

        var vis = $("geofence-visible");
        if (vis) {
            // Sync starting state in case the checkbox default and our
            // state default ever drift apart.
            state.visible = vis.checked;
            vis.addEventListener("change", function () {
                state.visible = vis.checked;
                // Redraw current profile honoring the new visibility.
                drawForProfile(activeProfile());
            });
        }
    }

    var Api = {
        init: function (map) {
            state.map = map;
            if (document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", wireUI);
            } else {
                wireUI();
            }
        },

        // Called from serverSettingsUpdate (settings.js / index.html) so
        // we always reflect the latest server state.
        onConfig: function (cfg) {
            syncCacheFromConfig(cfg);
            // Redraw unconditionally: the profile may have switched, or the
            // same profile's geofence may have changed underneath us.
            var profile = activeProfile();
            if (profile) drawForProfile(profile);
        },

        // SocketIO 'geofence_update' handler.
        onSocketUpdate: function (msg) {
            if (!msg || !msg.profile) return;
            state.cache[msg.profile] = msg.geofence || null;
            if (typeof msg.has_trash !== "undefined") {
                state.trashCache[msg.profile] = !!msg.has_trash;
            }
            if (msg.profile === activeProfile()) {
                // If a draw is in progress on this profile when an
                // update lands, don't yank the user out of draw mode —
                // the saved layer is hidden during draw anyway, and
                // they'll see the new state when they Finish/Cancel.
                if (state.drawing) {
                    updateButtons(!!state.cache[msg.profile]);
                } else {
                    drawForProfile(msg.profile);
                }
            }
        },

        // Called when the user picks a different profile from #profileSelect,
        // before the round-trip completes. Reads from cache.
        onProfileChange: function (profile) {
            // Switching profiles mid-draw would orphan vertices; cancel
            // cleanly first.
            if (state.drawing) teardownDrawMode();
            drawForProfile(profile);
        },

        _state: function () { return state; }
    };

    window.GeofenceOverlay = Api;
})();
