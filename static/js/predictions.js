//
//   Project Horus - Browser-Based Chase Mapper - Prediction Path Handlers
//
//   Copyright (C) 2019  Mark Jessop <vk5qi@rfhead.net>
//   Released under GNU GPL v3 or later
//

var launch_preview = {
    path: null,
    burst_marker: null,
    landing_marker: null,
    locked: false,
    running: false
};

function setLaunchPreviewStatus(message, isError){
    var status = document.getElementById("launchPreviewStatus");
    if (!status) return;
    status.textContent = message || "";
    status.style.color = isError ? "#dc2626" : "#6b7280";
}

function updateLaunchPreviewButton(){
    var button = document.getElementById("runLaunchPreview");
    if (!button) return;
    button.disabled = launch_preview.locked || launch_preview.running;
}

function setLaunchPreviewLocked(locked){
    launch_preview.locked = !!locked;
    updateLaunchPreviewButton();
    if (launch_preview.locked) {
        setLaunchPreviewStatus("Live balloon prediction active; launch preview locked.", false);
    }
}

function removeLaunchPreviewLayers(){
    [launch_preview.path, launch_preview.burst_marker, launch_preview.landing_marker]
        .forEach(function(layer){
            if (layer && typeof map !== "undefined" && map.hasLayer(layer)) {
                map.removeLayer(layer);
            }
        });
    launch_preview.path = null;
    launch_preview.burst_marker = null;
    launch_preview.landing_marker = null;
}

function handleLaunchPreview(data){
    removeLaunchPreviewLayers();

    launch_preview.path = L.polyline(data.pred_path, {
        title: "Launch Preview",
        color: "#0891b2",
        weight: 3,
        opacity: 0.9,
        dashArray: "8 6"
    }).addTo(map);

    if (data.burst && data.burst.length === 3) {
        launch_preview.burst_marker = L.circleMarker(data.burst, {
            radius: 7,
            color: "#c2410c",
            fillColor: "#fb923c",
            fillOpacity: 0.9,
            weight: 2
        }).bindTooltip(
            "Launch Preview Burst (" + data.burst[2].toFixed(0) + "m)",
            {permanent: false, direction: "right"}
        ).addTo(map);
    }

    if (data.pred_landing && data.pred_landing.length === 3) {
        var landingText = "Launch Preview Landing " +
            data.pred_landing[0].toFixed(5) + ", " +
            data.pred_landing[1].toFixed(5);
        launch_preview.landing_marker = L.circleMarker(data.pred_landing, {
            radius: 8,
            color: "#0e7490",
            fillColor: "#22d3ee",
            fillOpacity: 0.9,
            weight: 2
        }).bindTooltip(landingText, {permanent: false, direction: "right"})
          .addTo(map);
        launch_preview.landing_marker.on("click", function(e){
            textToClipboard(
                e.latlng.lat.toFixed(5) + ", " + e.latlng.lng.toFixed(5)
            );
        });
        if (window.RecoveryOverlays) {
            RecoveryOverlays.updateLandingPoint(
                data.pred_landing[0], data.pred_landing[1]
            );
            RecoveryOverlays.attachLandingPopup(
                launch_preview.landing_marker,
                data.pred_landing[0],
                data.pred_landing[1],
                "Launch Preview Landing"
            );
        }
    }

    launch_preview.running = false;
    updateLaunchPreviewButton();
    var sourceTime = new Date(data.source_time);
    var sourceLabel = isNaN(sourceTime.getTime())
        ? data.source_time
        : sourceTime.toLocaleString();
    setLaunchPreviewStatus(
        "Preview from " + data.source_callsign + " at " + sourceLabel +
        " (" + Number(data.ascent_rate).toFixed(1) + " m/s).",
        false
    );
}

function clearLaunchPreview(data){
    removeLaunchPreviewLayers();
    launch_preview.running = false;
    if (data && data.reason === "live_prediction") {
        launch_preview.locked = true;
        setLaunchPreviewStatus("Live balloon prediction active; preview removed.", false);
    } else if (data && data.reason === "payload_clear") {
        launch_preview.locked = false;
        setLaunchPreviewStatus("Launch preview unlocked.", false);
    } else {
        setLaunchPreviewStatus("", false);
    }
    updateLaunchPreviewButton();
}

function handleLaunchPreviewStatus(data){
    launch_preview.running = data.state === "running";
    if (data.state === "locked") launch_preview.locked = true;
    updateLaunchPreviewButton();
    setLaunchPreviewStatus(data.message || "", data.state === "error");
}

function handlePrediction(data){
    // We expect the fields: callsign, pred_path, pred_landing, and abort_path and abort_landing, if abort predictions are enabled.
    var _callsign = data.callsign;
    var _pred_path = data.pred_path;
    var _pred_landing = data.pred_landing;

    // Bridge predicted landing into the recovery overlays module so it can
    // refetch parcels and augment the marker popup with Maps links.
    if (_pred_landing && _pred_landing.length >= 2 && window.RecoveryOverlays) {
        RecoveryOverlays.updateLandingPoint(_pred_landing[0], _pred_landing[1]);
    }

    // It's possible (though unlikely) that we get sent a prediction track before telemetry data.
    // In this case, just return.
    if (balloon_positions.hasOwnProperty(data.callsign) == false){
        return;
    }

    // Add the landing marker if it doesnt exist.
    var _landing_text = _callsign + " Landing " + data.pred_landing[0].toFixed(5) + ", " + data.pred_landing[1].toFixed(5);
    if (balloon_positions[_callsign].pred_marker == null){
        balloon_positions[_callsign].pred_marker = L.marker(data.pred_landing,{title:_callsign + " Landing", icon: balloonLandingIcons[balloon_positions[_callsign].colour]})
            .bindTooltip(_landing_text ,{permanent:false,direction:'right'});
        if (balloon_positions[_callsign].visible == true){
            balloon_positions[_callsign].pred_marker.addTo(map);
            // Add listener to copy prediction coords to clipboard.
            balloon_positions[_callsign].pred_marker.on('click', function(e) {
                var _landing_pos_text = e.latlng.lat.toFixed(5) + ", " + e.latlng.lng.toFixed(5);
                textToClipboard(_landing_pos_text);
            });
        }
    }else{
        balloon_positions[_callsign].pred_marker.setLatLng(data.pred_landing);
        balloon_positions[_callsign].pred_marker.setTooltipContent(_landing_text);
    }
    if(data.burst.length == 3){
        // There is burst data!
        var _burst_txt = _callsign + " Burst (" + data.burst[2].toFixed(0) + "m)";
        if (balloon_positions[_callsign].burst_marker == null){
            balloon_positions[_callsign].burst_marker = L.marker(data.burst,{title:_burst_txt, icon: burstIcon})
                .bindTooltip(_burst_txt,{permanent:false,direction:'right'});

            if (balloon_positions[_callsign].visible == true){
                balloon_positions[_callsign].burst_marker.addTo(map);
            }
        }else{
            balloon_positions[_callsign].burst_marker.setLatLng(data.burst);
            balloon_positions[_callsign].burst_marker.setTooltipContent(_burst_txt);
        }
    }else{
        // No burst data, or we are in descent.
        if (balloon_positions[_callsign].burst_marker != null){
            // Remove the burst icon from the map.
            balloon_positions[_callsign].burst_marker.remove();
            balloon_positions[_callsign].burst_marker = null;
        }
    }
    // Update the predicted path.
    balloon_positions[_callsign].pred_path.setLatLngs(data.pred_path);

    if (data.abort_landing.length == 3){
        // Only update the abort data if there is actually abort data to show.
        if (balloon_positions[_callsign].abort_marker == null){
            balloon_positions[_callsign].abort_marker = L.marker(data.abort_landing,{title:_callsign + " Abort", icon: abortIcon})
            .bindTooltip(_callsign + " Abort Landing",{permanent:false,direction:'right'});
            if((chase_config.show_abort == true) && (balloon_positions[_callsign].visible == true)){
                balloon_positions[_callsign].abort_marker.addTo(map);
            }
        }else{
            balloon_positions[_callsign].abort_marker.setLatLng(data.abort_landing);
        }

        balloon_positions[_callsign].abort_path.setLatLngs(data.abort_path);
    }else{
        // Clear out the abort and abort marker data.
        balloon_positions[_callsign].abort_path.setLatLngs([]);

        if (balloon_positions[_callsign].abort_marker != null){
            balloon_positions[_callsign].abort_marker.remove();
            balloon_positions[_callsign].abort_marker = null;
        }
    }
    // Reset the prediction data age counter.
    pred_data_age = 0.0;

    // Update the routing engine.
    //if (balloon_currently_following === data.callsign){
    //    router.setWaypoints([L.latLng(chase_car_position.latest_data[0],chase_car_position.latest_data[1]), L.latLng(data.pred_landing[0], data.pred_landing[1])]);
    //}
}
