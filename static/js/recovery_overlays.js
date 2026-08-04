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

    // Every per-layer fact lives here and nowhere else: the checkbox id, the
    // pane, the draw order and the colour are all derived from this list, so
    // adding or renaming a layer is a one-line edit.
    //
    // Listed bottom-to-top. Broad en-route areas sit below terminal airspace,
    // which sits below the hazard areas a balloon most needs to see. Blues for
    // controlled airspace, reds/oranges for hazard areas, green for the
    // equipment-mandatory zones.
    var AIRSPACE = [
        { key: "cta",        color: "#0ea5e9", weight: 1, fillOpacity: 0.03 },
        { key: "tma",        color: "#7c3aed", weight: 2, fillOpacity: 0.04 },
        { key: "ctr",        color: "#1f4ed8", weight: 2, fillOpacity: 0.06 },
        { key: "atz",        color: "#64748b", weight: 2, fillOpacity: 0.06 },
        { key: "rmz_tmz",    color: "#10b981", weight: 2, fillOpacity: 0.05 },
        { key: "military",   color: "#f97316", weight: 2, fillOpacity: 0.10 },
        { key: "restricted", color: "#dc2626", weight: 2, fillOpacity: 0.10 }
    ];

    // Separate SVG panes keep transparent polygons clickable without a
    // full-map Canvas renderer swallowing clicks intended for lower layers.
    var AIRSPACE_BY_KEY = {};
    AIRSPACE.forEach(function (layer, index) {
        var slug = layer.key.replace(/_/g, "-");
        layer.toggleId = "toggle-" + slug;
        layer.pane = "airspace-" + slug;
        layer.zIndex = 410 + 10 * index;
        layer.style = {
            color: layer.color,
            weight: layer.weight,
            fillOpacity: layer.fillOpacity
        };
        AIRSPACE_BY_KEY[layer.key] = layer;
    });

    var AIRSPACE_LAYERS = AIRSPACE.map(function (layer) { return layer.key; });

    var PARCEL_STYLE = { color: "#ea580c", weight: 1, fillOpacity: 0.08 };
    var SEARCH_CIRCLE_STYLE = { color: "#dc2626", weight: 2, dashArray: "6,6", fill: false };

    var state = {
        map: null,
        landing: null,
        lastFetchedLanding: null,
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
    function mapsLinksHtml(lat, lon) {
        return (
            '<a href="' + googleMapsUrl(lat, lon) + '" target="_blank" rel="noopener">Google Maps</a> · ' +
            '<a href="' + appleMapsUrl(lat, lon) + '" target="_blank" rel="noopener">Apple Maps</a>'
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
        AIRSPACE.forEach(function (config) {
            var pane = state.map.getPane(config.pane) || state.map.createPane(config.pane);
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
            var sub = $(AIRSPACE_BY_KEY[layer].toggleId);
            if (!sub || !sub.checked) return;
            if (!$("toggle-airspace").checked) return;
            var leafletLayer = L.geoJSON(data, {
                pane: AIRSPACE_BY_KEY[layer].pane,
                style: AIRSPACE_BY_KEY[layer].style,
                interactive: true,
                onEachFeature: function (feature, lyr) {
                    // Built on open, not up front - a layer can carry thousands
                    // of features and the operator opens a handful of popups.
                    lyr.bindPopup(function () {
                        return buildAirspacePopup(layer, feature.properties);
                    });
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
        AIRSPACE.forEach(function (config) {
            var sub = $(config.toggleId);
            if (masterOn && sub && sub.checked) {
                showAirspaceLayer(config.key);
            } else {
                hideAirspaceLayer(config.key);
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

    function getRadiusKm() {
        var slider = $("parcel-radius");
        return slider ? parseFloat(slider.value) : 0.5;
    }

    function renderSearchCircle() {
        if (!state.landing) return;
        if (state.searchCircle) state.map.removeLayer(state.searchCircle);
        var radiusMeters = getRadiusKm() * 1000.0;
        state.searchCircle = L.circle(state.landing, Object.assign(
            { radius: radiusMeters }, SEARCH_CIRCLE_STYLE
        )).addTo(state.map);
    }

    function buildParcelPopup(props, sourceLabel) {
        var p = props || {};
        // Spanish cadastres publish no owner names, so the cadastral reference
        // is the way to identify a plot.
        var html = "<b>" + escapeHtml(p.ref || "(no reference)") + "</b>";
        if (p.area_m2) {
            html += "<br>" + Math.round(p.area_m2).toLocaleString() + " m&sup2;";
        }
        if (p.municipality) {
            html += "<br><small>Municipality " + escapeHtml(p.municipality) + "</small>";
        }
        if (sourceLabel) {
            html += "<br><small>" + escapeHtml(sourceLabel) + "</small>";
        }
        if (p.info_url) {
            html += '<br><small><a href="' + escapeHtml(p.info_url) +
                    '" target="_blank" rel="noopener">Cadastre record</a></small>';
        }
        html += "<br>" + mapsLinksHtml(
            state.landing ? state.landing[0] : 0,
            state.landing ? state.landing[1] : 0
        );
        return html;
    }

    function fetchParcels() {
        if (!$("toggle-parcels").checked) return;
        if (!state.landing) {
            setStatus("parcel-status", "Waiting for predicted landing point…");
            return;
        }
        var radius = getRadiusKm();
        state.lastFetchedLanding = state.landing;
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
                    // Being outside a cadastre's coverage is expected, not a
                    // fault; only flag genuine failures in red.
                    var expected = (
                        data.error_code === "outside_coverage" ||
                        data.error_code === "no_open_service" ||
                        data.error_code === "no_data"
                    );
                    setStatus("parcel-status", data.error, !expected);
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
                        // Built on open, not up front - a result can run to
                        // thousands of parcels and almost none get clicked.
                        lyr.bindPopup(function () {
                            return buildParcelPopup(feature.properties, data.source_label);
                        });
                    }
                }).addTo(state.map);
                var count = (data.features || []).length;
                var msg = count + " parcels within " + radius + " km";
                if (data.source_label) msg += " · " + data.source_label;
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

    // A prediction lands every ~15 s, far apart for the debounce to coalesce,
    // and the landing point usually shifts only metres between them. Refetching
    // the same parcels that often is what provokes the cadastre's rate limiting.
    function landingMovedEnoughToRefetch() {
        if (!state.lastFetchedLanding) return true;
        var movedMetres = state.map.distance(state.landing, state.lastFetchedLanding);
        return movedMetres > getRadiusKm() * 100.0;   // 10% of the search radius
    }

    // The sidebar legend swatches take their colour from the layer table, so a
    // colour is defined in exactly one place and the legend cannot lie.
    function colourSwatches() {
        var swatches = document.querySelectorAll(".airspace-swatch[data-layer]");
        Array.prototype.forEach.call(swatches, function (el) {
            var config = AIRSPACE_BY_KEY[el.getAttribute("data-layer")];
            if (config) el.style.color = config.color;
        });
    }

    function wireToggles() {
        $("toggle-airspace").addEventListener("change", syncAirspace);
        AIRSPACE.forEach(function (config) {
            var el = $(config.toggleId);
            if (el) el.addEventListener("change", syncAirspace);
        });
        colourSwatches();
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
                if (label) label.textContent = parseFloat(slider.value).toFixed(2) + " km";
                if ($("toggle-parcels").checked) debounceParcelFetch();
            });
            if (label) label.textContent = parseFloat(slider.value).toFixed(2) + " km";
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
                if (landingMovedEnoughToRefetch()) debounceParcelFetch();
            }
        },

        attachLandingPopup: function (marker, lat, lon, title) {
            if (!marker || typeof lat !== "number" || typeof lon !== "number") return;
            attachLandingPopup(marker, lat, lon, title);
        },

        // The per-layer checkbox ids, so callers that persist or replay the
        // toggles do not have to restate the layer list.
        toggleIds: function () {
            return AIRSPACE.map(function (config) { return config.toggleId; });
        },

        _state: function () { return state; }
    };

    window.RecoveryOverlays = Api;
})();
