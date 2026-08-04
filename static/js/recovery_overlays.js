//
// CHASE - Browser-Based Chase Mapper - Recovery Overlays
//
// Self-contained module exposing window.RecoveryOverlays.
// Adds toggleable ENAIRE (Spanish) airspace and parcel overlays plus
// platform-aware Maps links on the predicted landing marker.
//
//   Copyright (C) 2026 Huy Huong <huyhuong@umd.edu>
//   Released under GNU GPL v3 or later
//
(function () {
    "use strict";

    var AIRSPACE_LAYERS = ["ctr", "tma", "cta", "atz", "restricted", "military", "rmz_tmz"];

    // Blues for controlled airspace, reds/oranges for hazard areas, green for
    // the equipment-mandatory zones.
    var AIRSPACE_STYLE = {
        ctr:        { color: "#1f4ed8", weight: 2, fillOpacity: 0.06 },
        tma:        { color: "#7c3aed", weight: 2, fillOpacity: 0.04 },
        cta:        { color: "#0ea5e9", weight: 1, fillOpacity: 0.03 },
        atz:        { color: "#64748b", weight: 2, fillOpacity: 0.06 },
        restricted: { color: "#dc2626", weight: 2, fillOpacity: 0.10 },
        military:   { color: "#f97316", weight: 2, fillOpacity: 0.10 },
        rmz_tmz:    { color: "#10b981", weight: 2, fillOpacity: 0.05 }
    };

    // Separate SVG panes keep transparent polygons clickable without a
    // full-map Canvas renderer swallowing clicks intended for lower layers.
    // Broad en-route areas sit below terminal airspace, which sits below the
    // hazard areas a balloon most needs to see.
    var AIRSPACE_PANES = {
        cta:        { name: "airspace-cta", zIndex: 410 },
        tma:        { name: "airspace-tma", zIndex: 420 },
        ctr:        { name: "airspace-ctr", zIndex: 430 },
        atz:        { name: "airspace-atz", zIndex: 440 },
        rmz_tmz:    { name: "airspace-rmz-tmz", zIndex: 450 },
        military:   { name: "airspace-military", zIndex: 460 },
        restricted: { name: "airspace-restricted", zIndex: 470 }
    };

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
        airspaceFetching: {}
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
        state.airspaceFetching[layer] = true;
        return fetch("/airspace/" + layer)
            .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            })
            .then(function (data) {
                state.airspaceData[layer] = data;
                state.airspaceFetching[layer] = false;
                return data;
            })
            .catch(function (e) {
                state.airspaceFetching[layer] = false;
                setStatus("airspace-status", "Failed to load " + layer + ": " + e.message, true);
                return null;
            });
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // Vertical limits are normalised server-side. ENAIRE's own display string
    // ("SFC", "1000ft AGL", "FL145") is authoritative; the numeric feet are a
    // fallback for the handful of records that have no display string.
    function formatLimit(display, feet, datum, fallback) {
        if (display) return display;
        if (feet !== null && feet !== undefined) {
            return Math.round(feet) + " ft" + (datum ? " " + datum : "");
        }
        return fallback;
    }

    function buildAirspacePopup(layer, props) {
        var p = props || {};
        var html = "<b>" + escapeHtml(p.name || layer.toUpperCase()) + "</b>";

        var designator = [p.type_code, p.ident].filter(Boolean).join(" ");
        if (designator) html += " <small>(" + escapeHtml(designator) + ")</small>";

        html += "<br>" +
            escapeHtml(formatLimit(p.lower, p.lower_ft, p.lower_datum, "SFC")) +
            " &mdash; " +
            escapeHtml(formatLimit(p.upper, p.upper_ft, p.upper_datum, "not specified"));

        if (p.airspace_class) {
            html += "<br><small><b>Class:</b> " + escapeHtml(p.airspace_class) + "</small>";
        }
        if (p.schedule) {
            html += "<br><small><b>Active:</b> " + escapeHtml(p.schedule) + "</small>";
        }
        if (p.frequency) {
            html += "<br><small><b>Frequency:</b> " + escapeHtml(p.frequency) + "</small>";
        }
        if (p.zones && p.zones.length) {
            html += "<br><small><b>Mandatory:</b> " + escapeHtml(p.zones.join(", ")) + "</small>";
        }
        if (p.remarks) {
            var remarks = String(p.remarks);
            if (remarks.length > 240) remarks = remarks.slice(0, 240) + "…";
            html += "<br><small>" + escapeHtml(remarks) + "</small>";
        }

        return html;
    }

    function ensureAirspacePanes() {
        Object.keys(AIRSPACE_PANES).forEach(function (layer) {
            var config = AIRSPACE_PANES[layer];
            var pane = state.map.getPane(config.name) || state.map.createPane(config.name);
            pane.style.zIndex = String(config.zIndex);
        });
    }

    function showAirspaceLayer(layer) {
        if (state.airspaceLayers[layer]) {
            state.airspaceLayers[layer].addTo(state.map);
            return;
        }
        fetchAirspace(layer).then(function (data) {
            if (!data) return;
            var sub = $("toggle-" + layer.replace(/_/g, "-"));
            if (!sub || !sub.checked) return;
            if (!$("toggle-airspace").checked) return;
            var leafletLayer = L.geoJSON(data, {
                pane: AIRSPACE_PANES[layer].name,
                style: AIRSPACE_STYLE[layer],
                interactive: true,
                onEachFeature: function (feature, lyr) {
                    lyr.bindPopup(buildAirspacePopup(layer, feature.properties));
                }
            });
            state.airspaceLayers[layer] = leafletLayer;
            leafletLayer.addTo(state.map);
            setStatus("airspace-status", "Loaded " + (data.features || []).length + " " + layer + " features");
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
            var subId = "toggle-" + layer.replace(/_/g, "-");
            var sub = $(subId);
            if (masterOn && sub && sub.checked) {
                showAirspaceLayer(layer);
            } else {
                hideAirspaceLayer(layer);
            }
        });
    }

    function refreshAirspaceFromENAIRE() {
        var btn = $("airspace-refresh-btn");
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = "Refreshing…";
        setStatus("airspace-status", "Refreshing from ENAIRE…");

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
                AIRSPACE_LAYERS.forEach(function (layer) {
                    delete state.airspaceData[layer];
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
                    setStatus("airspace-status", "Refreshed from ENAIRE.");
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
                        var html =
                            "<b>" + owner + "</b><br>" +
                            (addr ? addr + "<br>" : "") +
                            (acct ? "<small>Acct: " + acct + "</small><br>" : "") +
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
            var el = $("toggle-" + layer.replace(/_/g, "-"));
            if (el) el.addEventListener("change", syncAirspace);
        });
        var refreshBtn = $("airspace-refresh-btn");
        if (refreshBtn) refreshBtn.addEventListener("click", refreshAirspaceFromENAIRE);

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
