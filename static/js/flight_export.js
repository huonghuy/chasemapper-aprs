//
//   CHASE - Browser-Based Chase Mapper - Flight KML Export
//
//   Released under GNU GPL v3 or later
//
//   Drives the "Flight Export" controls in the Settings pane. The KML
//   itself is assembled server-side from the chase log (see
//   chasemapper/kml_export.py); this module only picks which log and
//   hands the download to the browser.
//

var FlightExport = (function(){

    var state = {
        logs: [],
        payloads: [],
        busy: false
    };

    function el(id){
        return document.getElementById(id);
    }

    function setStatus(message, isError){
        var _status = el("exportKmlStatus");
        if (!_status){
            return;
        }
        _status.textContent = message || "";
        _status.style.color = isError ? "#b91c1c" : "#6b7280";
    }

    function formatSize(bytes){
        if (bytes < 1024){
            return bytes + " B";
        }
        if (bytes < 1024 * 1024){
            return (bytes / 1024).toFixed(0) + " kB";
        }
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function optionLabel(log){
        var _label = log.name + " (" + formatSize(log.size) + ")";
        return log.current ? _label + " - current" : _label;
    }

    function populate(logs){
        var _select = el("exportLogSelect");
        if (!_select){
            return;
        }

        // Keep whatever the operator had picked if it is still on disk.
        var _previous = _select.value;

        state.logs = logs || [];
        _select.innerHTML = "";

        if (state.logs.length === 0){
            var _empty = document.createElement("option");
            _empty.textContent = "No chase logs found";
            _empty.value = "";
            _select.appendChild(_empty);
            _select.disabled = true;
            setStatus("Nothing to export - logging may be disabled (--nolog).", false);
            return;
        }

        _select.disabled = false;
        for (var i = 0; i < state.logs.length; i++){
            var _option = document.createElement("option");
            _option.value = state.logs[i].name;
            _option.textContent = optionLabel(state.logs[i]);
            _select.appendChild(_option);
        }

        // Default to the session's own log, else the newest (the list
        // arrives newest-first).
        var _default = state.logs[0].name;
        for (var j = 0; j < state.logs.length; j++){
            if (state.logs[j].current){
                _default = state.logs[j].name;
                break;
            }
        }

        var _still_present = state.logs.some(function(log){
            return log.name === _previous;
        });
        _select.value = _still_present ? _previous : _default;
    }

    function refresh(){
        return fetch("/export/flights")
            .then(function(response){
                if (!response.ok){
                    throw new Error("HTTP " + response.status);
                }
                return response.json();
            })
            .then(function(data){
                populate(data.logs);
                return loadPayloads();
            })
            .catch(function(e){
                console.log("Could not list chase logs", e);
                setStatus("Could not list chase logs.", true);
            });
    }

    // ---- Payload picker -----------------------------------------------
    //
    // A chase log holds every balloon the APRS-IS filter matched, so the
    // operator picks which ones go in. Defaults to the active profile's
    // balloon callsigns from horusmapper.cfg.

    function payloadLabel(payload){
        var _bits = [payload.points + " packets"];
        if (payload.max_alt){
            _bits.push(Math.round(payload.max_alt) + " m max");
        }
        if (payload.prediction){
            _bits.push("launch prediction");
        }
        return payload.callsign + " - " + _bits.join(", ");
    }

    function renderPayloads(data){
        var _container = el("exportPayloads");
        if (!_container){
            return;
        }

        state.payloads = (data && data.payloads) || [];
        _container.innerHTML = "";

        if (state.payloads.length === 0){
            _container.textContent = "No payload telemetry in this log.";
            _container.style.color = "#6b7280";
            return;
        }

        _container.style.color = "";

        // Pre-select this profile's balloons. If the log predates the
        // current profile (none of its callsigns are in there), select
        // everything rather than handing back an empty export.
        var _any_in_profile = state.payloads.some(function(payload){
            return payload.in_profile;
        });

        for (var i = 0; i < state.payloads.length; i++){
            var _payload = state.payloads[i];

            var _label = document.createElement("label");
            _label.style.display = "block";

            var _box = document.createElement("input");
            _box.type = "checkbox";
            _box.className = "exportPayloadCheck";
            _box.value = _payload.callsign;
            _box.checked = _any_in_profile ? !!_payload.in_profile : true;

            _label.appendChild(_box);
            _label.appendChild(document.createTextNode(" " + payloadLabel(_payload)));

            if (!_payload.in_profile && _any_in_profile){
                _label.style.color = "#6b7280";
                _label.title = "Not a balloon callsign for the active profile.";
            }

            _container.appendChild(_label);
        }

        if (data.profile_callsigns && data.profile_callsigns.length > 0){
            var _note = document.createElement("div");
            _note.style.color = "#6b7280";
            _note.style.marginTop = "2px";
            _note.textContent =
                "Profile “" + data.profile + "” flies " +
                data.profile_callsigns.join(", ") + ".";
            _container.appendChild(_note);
        }
    }

    function loadPayloads(){
        var _select = el("exportLogSelect");
        var _container = el("exportPayloads");
        var _log = (_select && _select.value) || "";

        if (!_container){
            return Promise.resolve();
        }
        if (!_log){
            _container.innerHTML = "";
            state.payloads = [];
            return Promise.resolve();
        }

        _container.textContent = "Reading log…";
        _container.style.color = "#6b7280";

        return fetch("/export/payloads?log=" + encodeURIComponent(_log))
            .then(function(response){
                if (!response.ok){
                    throw new Error("HTTP " + response.status);
                }
                return response.json();
            })
            .then(renderPayloads)
            .catch(function(e){
                console.log("Could not read payload list", e);
                _container.textContent = "Could not read this log.";
                _container.style.color = "#b91c1c";
                state.payloads = [];
            });
    }

    function selectedCallsigns(){
        var _boxes = document.querySelectorAll(".exportPayloadCheck");
        var _selected = [];
        for (var i = 0; i < _boxes.length; i++){
            if (_boxes[i].checked){
                _selected.push(_boxes[i].value);
            }
        }
        return _selected;
    }

    function filenameFromResponse(response, fallback){
        var _disposition = response.headers.get("Content-Disposition") || "";
        var _match = /filename="([^"]+)"/.exec(_disposition);
        return _match ? _match[1] : fallback;
    }

    function saveBlob(blob, filename){
        var _url = URL.createObjectURL(blob);
        var _anchor = document.createElement("a");
        _anchor.href = _url;
        _anchor.download = filename;
        document.body.appendChild(_anchor);
        _anchor.click();
        document.body.removeChild(_anchor);
        // Revoking immediately can cancel the download in some browsers.
        setTimeout(function(){ URL.revokeObjectURL(_url); }, 30000);
    }

    function download(){
        if (state.busy){
            return;
        }

        var _select = el("exportLogSelect");
        var _button = el("exportKmlButton");
        var _log = (_select && _select.value) || "";

        if (!_log){
            setStatus("No chase log selected.", true);
            return;
        }

        var _callsigns = selectedCallsigns();
        if (state.payloads.length > 0 && _callsigns.length === 0){
            setStatus("Select at least one payload.", true);
            return;
        }

        state.busy = true;
        if (_button){
            _button.disabled = true;
        }
        setStatus("Building KML...", false);

        var _filename = "chasemapper_" + _log.replace(/\.log$/, "") + ".kml";

        var _query = "log=" + encodeURIComponent(_log);
        for (var i = 0; i < _callsigns.length; i++){
            _query += "&callsign=" + encodeURIComponent(_callsigns[i]);
        }

        fetch("/export/kml?" + _query)
            .then(function(response){
                if (!response.ok){
                    // Errors come back as JSON from the export routes.
                    return response.json()
                        .catch(function(){
                            return { error: "Export failed (HTTP " + response.status + ")." };
                        })
                        .then(function(data){
                            throw new Error(data.error || "Export failed.");
                        });
                }
                _filename = filenameFromResponse(response, _filename);
                return response.blob();
            })
            .then(function(blob){
                saveBlob(blob, _filename);
                setStatus("Downloaded " + _filename + ".", false);
            })
            .catch(function(e){
                setStatus(e.message || "Export failed.", true);
            })
            .then(function(){
                state.busy = false;
                if (_button){
                    _button.disabled = false;
                }
            });
    }

    function init(){
        var _button = el("exportKmlButton");
        var _select = el("exportLogSelect");

        if (!_button || !_select){
            return;
        }

        _button.addEventListener("click", download);
        _select.addEventListener("change", loadPayloads);
        // Logs roll over as flights happen, so re-read the directory
        // when the operator goes to pick one.
        _select.addEventListener("focus", refresh);

        refresh();
    }

    if (document.readyState === "loading"){
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    return {
        init: init,
        refresh: refresh,
        loadPayloads: loadPayloads,
        download: download
    };

})();
