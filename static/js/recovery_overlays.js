//
// CHASE - Browser-Based Chase Mapper - Recovery Overlays
//
// Self-contained module exposing window.RecoveryOverlays.
// Adds toggleable FAA airspace, TFR, and Maryland parcel overlays plus
// platform-aware Maps links on the predicted landing marker.
//
//   Copyright (C) 2026 Huy Huong <huyhuong@umd.edu>
//   Released under GNU GPL v3 or later
//
(function () {
    "use strict";

    // Single source of truth for the layer set. Listed in draw order, lowest
    // first: broad Class E areas sit below terminal airspace, SUA, and TFRs.
    // Iteration order, pane names, and pane z-index all derive from this.
    var AIRSPACE_LAYERS = ["class_e", "class_b", "class_c", "class_d", "sua", "tfr"];

    var AIRSPACE_STYLE = {
        class_b: { color: "#1f4ed8", weight: 2, fillOpacity: 0.05 },
        class_c: { color: "#7c3aed", weight: 2, fillOpacity: 0.05 },
        class_d: { color: "#0ea5e9", weight: 2, fillOpacity: 0.05 },
        class_e: { color: "#64748b", weight: 1, fillOpacity: 0.03 },
        sua:     { color: "#dc2626", weight: 2, fillOpacity: 0.08 },
        tfr:     { color: "#f97316", weight: 2, fillOpacity: 0.15 }
    };

    var AIRSPACE_PANE_BASE_Z = 410;

    var PARCEL_STYLE = { color: "#ea580c", weight: 1, fillOpacity: 0.08 };
    var SEARCH_CIRCLE_STYLE = { color: "#dc2626", weight: 2, dashArray: "6,6", fill: false };

    var state = {
        map: null,
        landing: null,
        airspaceData: {},
        airspaceLayers: {},
        parcelLayer: null,
        parcelCanvas: null,
        searchCircle: null,
        parcelTimer: null,
        airspaceFetching: {},
        // Bumped by every FAA refresh. An in-flight GET compares the value it
        // captured at request time before writing to state, so a response for
        // the pre-refresh cache can't land on top of the new one.
        airspaceGeneration: 0
    };

    function $(id) { return document.getElementById(id); }

    function setStatus(elId, text, isWarning) {
        var el = $(elId);
        if (!el) return;
        el.textContent = text || "";
        el.style.color = isWarning ? "#dc2626" : "#6b7280";
    }

    function googleMapsUrl(lat, lon) {
        return "https://www.google.com/maps/search/?api=1&query=" + lat + "," + lon;
    }
    function appleMapsUrl(lat, lon) {
        return "https://maps.apple.com/?ll=" + lat + "," + lon + "&q=" + lat + "," + lon;
    }
    function googleMapsAddrUrl(addr) {
        return "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(addr);
    }
    function appleMapsAddrUrl(addr) {
        return "https://maps.apple.com/?address=" + encodeURIComponent(addr);
    }

    function mapsLinksHtml(lat, lon, addr) {
        var g = addr ? googleMapsAddrUrl(addr) : googleMapsUrl(lat, lon);
        var a = addr ? appleMapsAddrUrl(addr) : appleMapsUrl(lat, lon);
        return (
            '<a href="' + g + '" target="_blank" rel="noopener">Google Maps</a> · ' +
            '<a href="' + a + '" target="_blank" rel="noopener">Apple Maps</a>'
        );
    }

    function fetchAirspace(layer) {
        if (state.airspaceData[layer] || state.airspaceFetching[layer]) {
            return Promise.resolve(state.airspaceData[layer]);
        }
        var generation = state.airspaceGeneration;
        state.airspaceFetching[layer] = true;
        return fetch("/airspace/" + layer)
            .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            })
            .then(function (data) {
                if (generation !== state.airspaceGeneration) return null;
                state.airspaceData[layer] = data;
                state.airspaceFetching[layer] = false;
                return data;
            })
            .catch(function (e) {
                if (generation !== state.airspaceGeneration) return null;
                state.airspaceFetching[layer] = false;
                setStatus("airspace-status", "Failed to load " + layer + ": " + e.message, true);
                return null;
            });
    }

    // Field-name fallbacks vary across FAA endpoints (Class_Airspace,
    // Special_Use_Airspace, TFR). Try common variants and stop at the first hit.
    function pickFirst(obj, keys) {
        for (var i = 0; i < keys.length; i++) {
            var v = obj[keys[i]];
            if (v !== undefined && v !== null && v !== "") return v;
        }
        return "";
    }

    // Popup detail rows all share one shape: skip empties, wrap in <small>,
    // escape on the way in so no call site can forget. htmlEscape comes from
    // overlays.js, which index.html loads ahead of this module.
    function popupRow(label, value) {
        // Matches pickFirst's notion of "absent": a numeric 0 is a real
        // altitude floor, not an empty row.
        if (value === "" || value === undefined || value === null) return "";
        var body = label ? "<b>" + label + ":</b> " + htmlEscape(value) : htmlEscape(value);
        return "<br><small>" + body + "</small>";
    }

    function formatAlt(value, unit, codeword) {
        if (value === "" || value === undefined || value === null) return "";
        var v = String(value).trim();
        var code = codeword ? String(codeword).trim().toUpperCase() : "";
        if (code === "SFC" || code === "UNLTD") return code;
        if (parseFloat(v) <= -9998 && code) return code;
        // Don't double-suffix if value already looks like "12000 FT" or "SFC".
        if (/[A-Za-z]/.test(v)) return v;
        var parts = [v];
        if (unit) parts.push(String(unit));
        if (code) parts.push(code);  // e.g. MSL, AGL
        return parts.join(" ");
    }

    function formatSchedule(props) {
        var explicit = pickFirst(props, ["TIMESOFUSE", "timesOfUse"]);
        if (explicit) return String(explicit);

        var code = String(pickFirst(props, ["WKHR_CODE", "wkhrCode"]) || "")
            .trim().toUpperCase();
        var remark = String(pickFirst(props, ["WKHR_RMK", "wkhrRemark"]) || "").trim();

        if (code === "H24") return "Continuous (H24)";
        if (code === "NOTAM") return "See NOTAM";

        // This FAA boilerplate describes where the legal schedule is defined;
        // it is not itself an operating-hours value.
        if (/legal description references notam/i.test(remark)) remark = "";
        return [code, remark].filter(Boolean).join(" — ");
    }

    function buildAirspacePopup(layer, props) {
        var p = props || {};
        var name = pickFirst(p, [
            "NAME", "name", "Name",
            "LOCAL_TYPE", "TYPE_CODE",
            "notam_id"
        ]) || layer.toUpperCase();

        var ceilVal = pickFirst(p, [
            "UPPER_VAL", "upperVal", "UPPER_ALT", "UPPER_LIMIT",
            "max_altitude", "maxAltitude", "MAX_ALT", "ceiling",
            "UPPER_DESC"
        ]);
        var ceilUnit = pickFirst(p, ["UPPER_UOM", "upperUom"]);
        var ceilCode = pickFirst(p, ["UPPER_CD", "UPPER_CODE", "UPPER_DESC_CODE"]);

        var floorVal = pickFirst(p, [
            "LOWER_VAL", "lowerVal", "LOWER_ALT", "LOWER_LIMIT",
            "min_altitude", "minAltitude", "MIN_ALT", "floor",
            "LOWER_DESC"
        ]);
        var floorUnit = pickFirst(p, ["LOWER_UOM", "lowerUom"]);
        var floorCode = pickFirst(p, ["LOWER_CD", "LOWER_CODE", "LOWER_DESC_CODE"]);

        var ceil = formatAlt(ceilVal, ceilUnit, ceilCode);
        var floor = formatAlt(floorVal, floorUnit, floorCode);

        var html = "<b>" + htmlEscape(name) + "</b>";
        if (floor || ceil) {
            html += "<br>" + htmlEscape(floor || "SFC") + " &mdash; " + htmlEscape(ceil || "?");
        }

        html += popupRow("Class/type", pickFirst(p, ["CLASS", "LOCAL_TYPE", "TYPE_CODE"]));
        html += popupRow("Sector", pickFirst(p, ["SECTOR", "sector"]));
        html += popupRow("Schedule", formatSchedule(p));
        html += popupRow("Controlling agency", pickFirst(p, ["CONT_AGENT", "COMM_NAME"]));

        if (layer === "tfr") {
            html += popupRow("State", pickFirst(p, ["STATE", "state"]));
        }

        // TFR-specific extras. airspace_cache normalizes the WFS schema into
        // these lowercase names, so no vendor spellings are probed here — and
        // there is no expiry row because the feed carries no expiry field.
        html += popupRow("NOTAM", p.notam_id);
        html += popupRow("", p.type);

        var tfrDesc = String(p.description || "");
        if (tfrDesc.length > 200) tfrDesc = tfrDesc.slice(0, 200) + "…";
        html += popupRow("", tfrDesc);

        html += popupRow("Last modified", p.last_modified);

        return html;
    }

    // Pane name and checkbox id are both mechanical transforms of the layer
    // key; keeping them adjacent is what stops the two from drifting.
    function airspacePaneName(layer) {
        return "airspace-" + layer.replace("_", "-");
    }

    function airspaceToggleId(layer) {
        return "toggle-" + layer.replace("_", "-");
    }

    // Separate SVG panes keep transparent polygons clickable without a
    // full-map Canvas renderer swallowing clicks intended for lower layers.
    function ensureAirspacePanes() {
        AIRSPACE_LAYERS.forEach(function (layer, i) {
            var name = airspacePaneName(layer);
            var pane = state.map.getPane(name) || state.map.createPane(name);
            pane.style.zIndex = String(AIRSPACE_PANE_BASE_Z + i * 10);
        });
    }

    function showAirspaceLayer(layer) {
        if (state.airspaceLayers[layer]) {
            state.airspaceLayers[layer].addTo(state.map);
            return;
        }
        fetchAirspace(layer).then(function (data) {
            if (!data) return;
            if (!$(airspaceToggleId(layer)).checked) return;
            if (!$("toggle-airspace").checked) return;
            var leafletLayer = L.geoJSON(data, {
                pane: airspacePaneName(layer),
                style: AIRSPACE_STYLE[layer],
                onEachFeature: function (feature, lyr) {
                    lyr.bindPopup(buildAirspacePopup(layer, feature.properties));
                }
            });
            state.airspaceLayers[layer] = leafletLayer;
            leafletLayer.addTo(state.map);
            setStatus("airspace-status", "Loaded " + (data.features || []).length + " " + layer + " features");
        }).catch(function (e) {
            // fetchAirspace absorbs network errors, so anything reaching here
            // came from rendering — a malformed cached geometry, or a missing
            // htmlEscape if overlays.js failed to load. Without this the
            // toggle stays checked with nothing drawn and no explanation.
            setStatus("airspace-status", "Failed to draw " + layer + ": " + e.message, true);
        });
    }

    function hideAirspaceLayer(layer) {
        if (state.airspaceLayers[layer]) {
            state.map.removeLayer(state.airspaceLayers[layer]);
        }
    }

    function syncAirspace() {
        var masterOn = $("toggle-airspace").checked;
        AIRSPACE_LAYERS.forEach(function (layer) {
            var sub = $(airspaceToggleId(layer));
            if (masterOn && sub && sub.checked) {
                showAirspaceLayer(layer);
            } else {
                hideAirspaceLayer(layer);
            }
        });
    }

    function refreshAirspaceFromFAA() {
        var btn = $("airspace-refresh-btn");
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = "Refreshing…";
        setStatus("airspace-status", "Refreshing from FAA…");

        fetch("/airspace/refresh", { method: "POST" })
            .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            })
            .then(function (data) {
                if (data.already_running) {
                    setStatus("airspace-status", "A refresh is already in progress.");
                    return;
                }
                // Clear cached payloads + drawn layers so syncAirspace re-fetches the new data.
                // The generation bump retires any GET still in flight for the
                // old cache; clearing airspaceFetching lets syncAirspace start
                // a fresh one instead of short-circuiting on the stale flag.
                state.airspaceGeneration++;
                AIRSPACE_LAYERS.forEach(function (layer) {
                    delete state.airspaceData[layer];
                    delete state.airspaceFetching[layer];
                    if (state.airspaceLayers[layer]) {
                        state.map.removeLayer(state.airspaceLayers[layer]);
                        delete state.airspaceLayers[layer];
                    }
                });
                syncAirspace();
                var results = data.results || {};
                var failed = AIRSPACE_LAYERS.filter(function (l) { return results[l] === false; });
                if (failed.length) {
                    setStatus("airspace-status", "Refreshed (failed: " + failed.join(", ") + ")", true);
                } else {
                    setStatus("airspace-status", "Refreshed from FAA.");
                }
            })
            .catch(function (e) {
                setStatus("airspace-status", "Refresh failed: " + e.message, true);
            })
            .then(function () {
                btn.disabled = false;
                btn.textContent = originalText;
            });
    }

    function clearParcels() {
        if (state.parcelLayer) {
            state.map.removeLayer(state.parcelLayer);
            state.parcelLayer = null;
        }
        if (state.searchCircle) {
            state.map.removeLayer(state.searchCircle);
            state.searchCircle = null;
        }
    }

    function getRadiusMiles() {
        var slider = $("parcel-radius");
        return slider ? parseFloat(slider.value) : 0.5;
    }

    function renderSearchCircle() {
        if (!state.landing) return;
        if (state.searchCircle) state.map.removeLayer(state.searchCircle);
        var radiusMeters = getRadiusMiles() * 1609.344;
        state.searchCircle = L.circle(state.landing, Object.assign(
            { radius: radiusMeters }, SEARCH_CIRCLE_STYLE
        )).addTo(state.map);
    }

    function fetchParcels() {
        if (!$("toggle-parcels").checked) return;
        if (!state.landing) {
            setStatus("parcel-status", "Waiting for predicted landing point…");
            return;
        }
        var radius = getRadiusMiles();
        renderSearchCircle();
        var url = "/parcels?lat=" + state.landing[0] +
                  "&lon=" + state.landing[1] +
                  "&radius=" + radius;
        setStatus("parcel-status", "Loading parcels…");
        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!$("toggle-parcels").checked) return;
                if (data.error) {
                    if (state.parcelLayer) {
                        state.map.removeLayer(state.parcelLayer);
                        state.parcelLayer = null;
                    }
                    setStatus(
                        "parcel-status",
                        data.error,
                        data.error_code !== "outside_md"
                    );
                    return;
                }
                if (state.parcelLayer) {
                    state.map.removeLayer(state.parcelLayer);
                }
                if (!state.parcelCanvas) state.parcelCanvas = L.canvas();
                state.parcelLayer = L.geoJSON(data, {
                    renderer: state.parcelCanvas,
                    style: PARCEL_STYLE,
                    onEachFeature: function (feature, lyr) {
                        var p = feature.properties || {};
                        var owner = p.OWNNAME1 || "(no owner)";
                        var addr = p.PREMISEADD || "";
                        var acct = p.ACCTID || "";
                        // Owner, address, and account id are third-party
                        // strings from the Maryland parcel service, rendered
                        // into a page holding the RECOVERY_API_KEY session.
                        var html =
                            "<b>" + htmlEscape(owner) + "</b><br>" +
                            (addr ? htmlEscape(addr) + "<br>" : "") +
                            (acct ? "<small>Acct: " + htmlEscape(acct) + "</small><br>" : "") +
                            mapsLinksHtml(0, 0, addr || (state.landing && (state.landing[0] + "," + state.landing[1])));
                        lyr.bindPopup(html);
                    }
                }).addTo(state.map);
                var count = (data.features || []).length;
                var msg = count + " parcels within " + radius + " mi";
                if (data._truncated) msg += " (TRUNCATED, results capped)";
                setStatus("parcel-status", msg, !!data._truncated);
            })
            .catch(function (e) {
                setStatus("parcel-status", "Parcel fetch failed: " + e.message, true);
            });
    }

    function debounceParcelFetch() {
        if (state.parcelTimer) clearTimeout(state.parcelTimer);
        state.parcelTimer = setTimeout(fetchParcels, 400);
    }

    function wireToggles() {
        $("toggle-airspace").addEventListener("change", syncAirspace);
        AIRSPACE_LAYERS.forEach(function (layer) {
            var el = $(airspaceToggleId(layer));
            if (el) el.addEventListener("change", syncAirspace);
        });
        var refreshBtn = $("airspace-refresh-btn");
        if (refreshBtn) refreshBtn.addEventListener("click", refreshAirspaceFromFAA);

        $("toggle-parcels").addEventListener("change", function () {
            if (this.checked) {
                debounceParcelFetch();
            } else {
                clearParcels();
                setStatus("parcel-status", "");
            }
        });

        var slider = $("parcel-radius");
        var label = $("parcel-radius-val");
        if (slider) {
            slider.addEventListener("input", function () {
                if (label) label.textContent = parseFloat(slider.value).toFixed(2) + " mi";
                if ($("toggle-parcels").checked) debounceParcelFetch();
            });
            if (label) label.textContent = parseFloat(slider.value).toFixed(2) + " mi";
        }
    }

    function attachLandingPopup(marker, lat, lon, title) {
        // Augment existing prediction marker popup with Maps links.
        var html =
            "<b>" + (title || "Predicted Landing") + "</b><br>" +
            lat.toFixed(5) + ", " + lon.toFixed(5) + "<br>" +
            mapsLinksHtml(lat, lon);
        marker.bindPopup(html);
    }

    var Api = {
        init: function (map) {
            state.map = map;
            ensureAirspacePanes();
            // Defer wiring until DOM elements exist; index.html may load this script
            // before the sidebar HTML is parsed.
            if (document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", wireToggles);
            } else {
                wireToggles();
            }
        },

        updateLandingPoint: function (lat, lon) {
            if (typeof lat !== "number" || typeof lon !== "number") return;
            state.landing = [lat, lon];

            // Augment any existing prediction markers with Maps links.
            if (typeof balloon_positions !== "undefined") {
                for (var cs in balloon_positions) {
                    var bp = balloon_positions[cs];
                    if (bp && bp.pred_marker) {
                        attachLandingPopup(bp.pred_marker, lat, lon);
                    }
                }
            }

            if ($("toggle-parcels") && $("toggle-parcels").checked) {
                debounceParcelFetch();
            }
        },

        attachLandingPopup: function (marker, lat, lon, title) {
            if (!marker || typeof lat !== "number" || typeof lon !== "number") return;
            attachLandingPopup(marker, lat, lon, title);
        },

        _state: function () { return state; }
    };

    window.RecoveryOverlays = Api;
})();
