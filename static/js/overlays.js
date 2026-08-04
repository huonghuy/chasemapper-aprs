//
//   Project Horus - Browser-Based Chase Mapper - Map Overlay Handlers
//
//   Released under GNU GPL v3 or later
//

function htmlEscape(value){
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}


function kmlColourToCss(value){
    // leaflet-omnivore strips the alpha byte off a KML colour but does not
    // re-order the rest, and KML stores colours as bbggrr. Swap back to rrggbb.
    var _hex = String(value).replace("#", "");

    if (!/^[0-9a-fA-F]{6}$/.test(_hex)){
        return value;
    }

    return "#" + _hex.substr(4, 2) + _hex.substr(2, 2) + _hex.substr(0, 2);
}


function kmlFeatureStyle(feature){
    // Apply the LineStyle / PolyStyle values omnivore pulled out of the KML.
    // Without this Leaflet ignores them and draws everything in default blue.
    var _props = feature.properties || {};
    var _style = {};

    if (_props["stroke"] !== undefined){
        _style.color = kmlColourToCss(_props["stroke"]);
    }
    if (_props["stroke-width"] !== undefined){
        _style.weight = parseFloat(_props["stroke-width"]);
    }
    if (_props["stroke-opacity"] !== undefined){
        _style.opacity = parseFloat(_props["stroke-opacity"]);
    }
    if (_props["fill"] !== undefined){
        _style.fillColor = kmlColourToCss(_props["fill"]);
    }
    if (_props["fill-opacity"] !== undefined){
        _style.fillOpacity = parseFloat(_props["fill-opacity"]);
    }

    return _style;
}


function kmlPointToLayer(feature, latlng){
    // Draw KML points as circle markers so they can't be confused with the
    // balloon and chase-car markers. Colour comes from the simplestyle
    // ExtendedData keys, as omnivore does not parse KML IconStyle.
    var _props = feature.properties || {};
    var _colour = _props["marker-color"] || "#3388ff";

    return L.circleMarker(latlng, {
        radius: (_props["marker-size"] === "large") ? 7 : 4,
        color: _colour,
        weight: 2,
        fillColor: _colour,
        fillOpacity: 0.6
    });
}


function bindKmlPopup(feature, layer){
    if (!feature.properties){
        return;
    }

    var _popup_parts = [];
    if (feature.properties.name){
        _popup_parts.push("<b>" + htmlEscape(feature.properties.name) + "</b>");
    }
    if (feature.properties.description){
        _popup_parts.push(htmlEscape(feature.properties.description));
    }

    if (_popup_parts.length > 0){
        layer.bindPopup(_popup_parts.join("<br>"));
    }
}


function loadConfiguredKmlOverlays(config, map){
    var _overlay_layers = {};

    if (!config.hasOwnProperty("kml_overlays") || config.kml_overlays.length == 0){
        return _overlay_layers;
    }

    if (typeof omnivore === "undefined"){
        console.log("KML overlays configured, but leaflet-omnivore is not loaded.");
        return _overlay_layers;
    }

    for (var i = 0, len = config.kml_overlays.length; i < len; i++) {
        var _overlay = config.kml_overlays[i];
        var _custom_layer = L.geoJson(null, {
            onEachFeature: bindKmlPopup,
            style: kmlFeatureStyle,
            pointToLayer: kmlPointToLayer
        });

        var _layer = omnivore.kml(
            "/overlays/kml/" + encodeURIComponent(_overlay.id),
            null,
            _custom_layer
        );

        _layer.on("error", function(e) {
            console.log("Error loading KML overlay", e);
        });

        _overlay_layers[_overlay.name] = _layer;

        if (_overlay.visible == true){
            _layer.addTo(map);
        }
    }

    return _overlay_layers;
}
