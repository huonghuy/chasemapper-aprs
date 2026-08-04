# CHASE - Browser-Based HAB Chase Map for University of Maryland Balloon Payload Program (BPP)

Original fork comes from https://github.com/projecthorus/chasemapper, which enabled offline live predicitions. If you want to learn more about that system and how to integrate it raw, visit thier repo and show them support!

CHASE is reworked into being used for the UMD BPP, with the following features:
* [APRS-IS Packet Support!](https://github.com/lightaprs/LightAPRS-2.0)
* [Spot Trace Packet Support!](https://www.findmespot.com/en-us/products-services/spot-trace)
* [No Iridium Support because that's too much money](https://www.groundcontrol.com/products/iridium-messaging-transport-imt-pricing/)
* [Choppies Geofence Overlay](https://loonatec.com/product/balloon-cut-down-system/)
* MD/PA/NJ/WV/NY Airspace Overlays
* MD Parcel information for contacting land owners
* Live Standard/GHOUL Landing Prediction + tracking!

NOTE: This package was designed to be in tandom of the [current prediction website](https://bpp.umd.edu/develop/predicts/) , not as a replacement. The thought being the program uses the prediction website leading up to a launch and CHASE the day of the launch. After five years of participating in HAB launches and research, I was tired of swapping between several apps and websites and message threads to retreive tracking information and then spending more time than needed typing in coordinates to landing sites and trying to figure out many more logistics. 

This was done out of a labour of love and hopefully will be useful for the future of this and many other programs!

![ChaseMapper Screenshot](https://github.com/projecthorus/chasemapper/raw/master/doc/chasemapper.jpg)

The primary purpose of chasemapper is to provide an easy-to-use mapping interface to help you as close as possible to the landing location of a high-altitude balloon payload, ideally before the payload gets there so you can watch it land! It does this by providing live predictions of the balloon flight path during the flight, calculated from GFS weather models which are downloaded before you head off on the chase. Maps can also be served up from a local cache, allowing use without internet connectivity (useful here in Australia!). 

Chasemapper is intended to be run on a 'headless' machine like a Raspberry Pi and is accessed from a tablet or laptop computer via a web browser. Multiple clients can connect to the server to see what's going on, which is a nice way of keeping passengers entertained ;-)

### Contacts
* [Huy Huong](https://github.com/huonghuy) - huyhuong@umd.edu

## Regional Limitations

A few overlays are hardcoded to the mid-Atlantic and will be empty
or unavailable outside that region:

* **FAA airspace + TFR overlays** — limited to the bounding box covering
  MD/PA/DE/VA/WV (see `REGION_BBOX` in `chasemapper/airspace_cache.py`).
  Toggling airspace on outside that area shows nothing.
* **Parcel lookup** — uses the Maryland statewide parcel service and
  only returns results inside MD.

Everything else (APRS-IS, SPOT, predictions, maps) is location-agnostic.

## Quickstart (Docker)

The fastest path from zero to a running map.
```bash
git clone https://github.com/huonghuy/chasemapper-aprs.git
cd chasemapper-aprs
```

1. Create your config
```bash
cp horusmapper.cfg.example horusmapper.cfg
# Edit horusmapper.cfg — set at least:
#   - default_lat / default_lon (map center)
#   - your APRS-IS profile (callsigns for cars + balloons)
```

2. Create your .env (see "Environment Variables" section below)
```bash
touch .env
# If you're not exposing this publicly, you can leave .env empty —
# the SPOT + RECOVERY_API_KEY features just stay disabled.
```

3. Create a docker-compose.yml from the example
```bash
cp docker-compose.yml.example docker-compose.yml
# Edit if you want to build locally instead of pulling the image,
# or to add an offline map tile mount.
```

4. Start it
```bash
docker compose up -d
```

5. Open the UI at `http://<host-ip>:5001/`

To see logs: `docker compose logs -f chasemapper`. To stop:
`docker compose down`. To pull a newer build:
`docker compose pull && docker compose up -d`.

### Expected startup noise

These messages appear on a fresh boot and are **harmless** — they are
not errors with your setup:

* `GFS Data in directory does not cover now!` — no offline GFS model
  downloaded yet. Online predictions still work via SondeHub. If you
  want offline predictions, click "Download Model" in the Settings tab.
* `Unable to read in last position` — no prior flight log to resume
  from. Disappears after the first received packet.
* `SPOT: <feed> skipped — env var ... is not set` — SPOT feed IDs not
  configured in `.env`. Only matters if you're tracking a SPOT Trace.
* `SyntaxWarning: invalid escape sequence '\!'` from `aprslib` — a
  cosmetic warning in an upstream library, not our code.

## Docker Install (detailed)
The above Quickstart covers the common case. If you want background on
the upstream Docker setup (advanced volume mounts, offline mapping,
GFS download flags), see: https://github.com/projecthorus/chasemapper/wiki/Docker
Note that the upstream wiki predates the APRS-IS / SPOT / airspace /
parcel features in this fork.


## 'Local' Install - Dependencies
If you are using Docker, you can skip this section.

**Note: ChaseMapper requires Python 3.6 or newer.**

On a Raspbian/Ubuntu/Debian system, install the system-level build deps:
```bash
$ sudo apt-get install git python3-pip libatlas3-base libgfortran5 libopenblas-dev libgeos-dev
```
On other OSes the required packages should be named something similar.

Clone the repo:
```bash
$ git clone https://github.com/huonghuy/chasemapper-aprs.git
$ cd chasemapper-aprs
```

Install the Python dependencies from `requirements.txt` (covers
flask, flask-socketio, aprslib, pytz, requests, numpy, etc.):
```bash
$ pip3 install -r requirements.txt
```
A virtualenv is recommended if you don't want to install into the
system Python.

## Telemetry Sources
To use the map, you need some kind of data to plot on it! The mapping backend accepts telemetry data in a few formats:
* 'Payload Summary', 'Chase Car Position' and 'Bearing' messages, via UDP broadcast in a JSON format [described here](https://github.com/projecthorus/horus_utils/wiki/5.-UDP-Broadcast-Messages#payload-summary-payload_summary). The standard ports used for these are 55672 (for hobbyist HAB payloads). These can be generated by:
  * The [Horus-GUI](https://github.com/projecthorus/horus-gui) and [Horus Binary](https://github.com/projecthorus/horusdemodlib/wiki) 4FSK telemetry decoders will emit these messages on port 55672 by default.
* 'OziMux' messages, via UDP broadcast in a simple CSV format [described here](https://github.com/projecthorus/oziplotter/wiki/3---Data-Sources#3---oziplotter-data-inputs).
  * Pi-in-the-Sky's [lora_gateway](https://github.com/PiInTheSky/lora-gateway) - Using the `OziPort=8942` configuration option.

## Environment Variables (.env)

Chasemapper reads secrets from environment variables to keep them out of
the committed config. Create a `.env` file in the repo root (already
gitignored):
```bash
touch .env
```

### Variables

| Variable             | Required?  | Purpose                                       |
| -------------------- | ---------- | --------------------------------------------- |
| `RECOVERY_API_KEY`   | Optional   | Auth token for FAA airspace / TFR / MD parcel |
|                      |            | overlays. Set only if running behind          |
|                      |            | Cloudflare with X-Recovery-Key injection.     |
| `SPOT_FEED_COMMAND`  | Optional   | SPOT public-feed ID for the "command"         |
|                      |            | tracker.                                      |
| `SPOT_FEED_HAPL`     | Optional   | SPOT public-feed ID for the "HAPL" tracker.   |

Unset variables disable the corresponding feature — chasemapper logs a
warning and continues.

### Finding a SPOT Feed ID

1. Sign in at https://www.findmespot.com
2. Open your device → **Share** → create or open a **Shared Page**
3. The Feed ID is the long alphanumeric token in the page URL
4. Paste each into `.env`:
```bash
RECOVERY_API_KEY=...
SPOT_FEED_COMMAND=XXXXXXXXXXXXXXXXXXXX
SPOT_FEED_HAPL=YYYYYYYYYYYYYYYYYYYY
```

### Wiring it into docker-compose

Add `env_file:` to the chasemapper service in your `docker-compose.yml`:
```yaml
services:
  chasemapper:
    # ... your existing config ...
    env_file:
      - .env
```

Then start as usual:
```bash
docker compose up -d
```

### Verify

```bash
docker compose logs chasemapper | grep SPOT
```

You should see:

```
SPOT: started listener for 2 feed(s), poll interval 300s
```

If you see `SPOT: <callsign> skipped — env var ... is not set`, the
variable isn't reaching the container — check `env_file:` is set on the
right service and the `.env` file is in the directory you run
`docker compose` from.

## Configuration & Startup
Many settings are defined in the [horusmapper.cfg](./horusmapper.cfg.example) configuration file.
Create a copy of the example config file using
```bash
$ cp horusmapper.cfg.example horusmapper.cfg
```
Edit this file with your preferred text editor. The configuration file is fairly descriptive - you will need to set:
 * At least one telemetry 'profile', which defines where payload and (optionally) car position telemetry data is sourced from.
 * A default latitude and longitude for the map to centre on.

The example configuration file includes profiles suitable for receiving data from radiosonde_auto_rx, and from [Horus-GUI](https://github.com/projecthorus/horus-gui).

You need to create a .env file if routing this through some back end. This will hold currently your
- cloudflare key
- SPOT Trace Key!

Once configured, you can start-up the horusmapper server with:
```bash
$ python3 horusmapper.py
```

The server can be stopped with CTRL+C. Sometimes the server doesn't stop cleanly and may the process may need to be killed. (Sorry!)

You should then be able to access the webpage by visiting http://your_ip_here:5001/

## Live Predictions
By default, chasemapper will attempt to request flight-path predictions from the SondeHub instance of the [Tawhiri Predictor](https://github.com/projecthorus/tawhiri), which requires an internet connection. If you have a semi-reliable internet connection during the flight, this might be all you need to get chasing!

However, if you think you might be going out of phone coverage range, you may want to set up offline predictions:

### Offline Predictions
To do this you need cusf_predictor_wrapper and it's dependencies installed. Refer to the [documentation on how to install this](https://github.com/darksidelemm/cusf_predictor_wrapper/). If you are using Docker, you can skip this section as it will already be set up.

Once compiled and the python library installed, you will need to: 
 * Copy the 'pred' binary into this directory. If using the Windows build, this will be `pred.exe`; under Linux/OSX, just `pred`.

You will then need to modify the horusmapper.cfg Predictor section setting as necessary to reflect the predictory binary location, the appropriate model_download command.

You can then click 'Download Model' in the web interface's setting tab to trigger a download of the latest GFS model data. Offline predictions will start automatically once a valid model is available. You can tell if you are using Online or Offline predictions by an '(Online)' or '(Offline)' indication next to the 'Current Model' line in the status panel.

## Chase Car Positions
At the moment Chasemapper supports receiving chase-car positions via either GPSD, a Serial-attached GPS, or Horus UDP messages. Refer to the configuration file for setup information for these options.

This application can also plot your position onto the tracker.habhub.org map, so others can see when you're out balloon chasing. You can also fetch positions of nearby chase cars from SondeHub/SondeHub-Amateur, to see if others are out chasing as well :-) These options can be enabled from the control pane on the left of the web interface, and can also be set within the configuration file. 

## Offline Mapping 
Chasemapper can serve up map tiles from a specified directory to the web client. Of course, for this to be useful, we need map tiles to serve! 

Serving of local map tiles can be enabled by setting `[offline_maps] tile_server_enabled = True`, and changing `[offline_maps] tile_server_path` to point to your tile cache directory (i.e. `/home/pi/Maps/`). Chasemapper will assume each subdirectory in this folder is a valid map layer (e.g. `~/Maps/OSM/`, `~/Maps/opencyclemap/`). and will add them to the map layer list at the top-right of the interface.

Note that if you want to use these offline maps within a Docker container, you will need to [modify the tile server path](https://github.com/projecthorus/chasemapper/blob/master/horusmapper.cfg.example#L185) in your configuration file to be /opt/chasemapper/Maps/ 

### Option 1 - MapTilesDownloader
[MapTilesDownloader](https://github.com/ke5gdb/MapTilesDownloader) can be setup on your RPi, allowing access via a web browser to select tile regions. KE5GDB's fork (linked above) has docker images available for easy setup.

To do a once-off startup of MapTilesDownloader and grab some tiles, run:
```bash
docker run \
  -t \
  --name maptilesdownloader \
  --network=host \
  -v ~/Maps/:/opt/MapTilesDownloader/output/ \
  ghcr.io/ke5gdb/maptilesdownloader:testing
```
.. then navigate to port 5002 on on your RPi's IP address to see the web interface.

To make it run on every boot, run:
```bash
docker run \
  -d \
  -t \
  --restart=always \
  --name maptilesdownloader \
  --network=host \
  -v ~/Maps/:/opt/MapTilesDownloader/output/ \
  ghcr.io/ke5gdb/maptilesdownloader:testing
```

Caching map tiles down to zoom level 15 is usually sufficient.

### Option 2 - FoxtrotGPS's Tile Cache
Another (less preferred) option to obtain map tiles is [FoxtrotGPS](https://www.foxtrotgps.org/).

To grab map tiles using FoxtrotGPS, we're going to use FoxtrotGPS's [Cached Maps](https://www.foxtrotgps.org/doc/foxtrotgps.html#Cached-Maps) feature. 

 * Install FoxtrotGPS (Linux only unfortunately, works OK on a Pi!) either [from source](https://www.foxtrotgps.org/releases/), or via your system package manager (`sudo apt-get install foxtrotgps`). 
 * Warning - Installing foxtrotgps will also install gpsd, which may 'take over' your GPS receiver! If you aren't using GPSD, I'd recommend uninstalling it with: `sudo apt-get purge gpsd` 
 * Load up FoxtrotGPS, and pan around the area you are intersted in caching. Pick the map layer you want, right-click on the map, and choose 'Map download'. You can then select how many zoom levels you want to cache, and start it downloading (this may take a while!)
 * Once you have a set of folders within your `~/Maps` cache directory, you can startup Chasemapper and start using them! Tiles will be served up as they become available.

## Running as a Systemd Service
Chasemapper can be operated in a 'continuous' mode, running as a systemd service. I use this in my chase car so that I can power up my car Raspberry Pi, and have services like auto_rx and chasemapper running immediately. 

If you're using docker, this is already sorted out for you, and the docker container will run at startup.

To set this up, the chasemapper.service file  must be edited to include your username, and the path to this directory.

```bash
sudo cp chasemapper.service /etc/systemd/system/
sudo nano /etc/systemd/system/chasemapper.service
```

If you are not running chasemapper on a Raspberry Pi as the 'pi' user, you will need to edit the chasemapper.service file and modify
the `ExecStart`, `WorkingDirectory` and `User` fields. Otherwise, leave all settings at their defaults:

```bash
[Unit]
Description=chasemapper
After=syslog.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/chasemapper/horusmapper.py
Restart=always
RestartSec=3
WorkingDirectory=/home/pi/chasemapper/
User=pi
SyslogIdentifier=chasemapper

[Install]
WantedBy=multi-user.target
```

Once/if edited, install and start the service using:
```bash
$ sudo systemctl enable chasemapper.service
$ sudo systemctl start chasemapper.service
```

The debug log output can be viewed buy running:
```bash
$ sudo journalctl -u chasemapper.service -f -n
```

To stop the service, simply run:
```bash
$ sudo systemctl stop chasemapper.service
```

## RECOVERY_API_KEY Setup

`RECOVERY_API_KEY` is an optional shared secret that gates the recovery
overlay endpoints — FAA airspace layers, TFRs, Maryland property parcels,
and per-profile geofence upload/clear. It's there to prevent the public
internet from hammering these (some of them proxy external APIs with
rate limits or cost).

It is **not** issued by anyone — you generate the string yourself and
plant the same value on both ends (server and Cloudflare).

### When you need it

| Deployment                                          | Need it?         |
| --------------------------------------------------- | ---------------- |
| Local / LAN-only chase laptop                       | No — leave unset |
| Exposed publicly (custom domain, behind Cloudflare) | Yes              |
| Behind a VPN / Tailscale only                       | Optional         |

When unset, the recovery endpoints are open. When set, every request to
them must carry `X-Recovery-Key: <your value>` or the server returns
`403 Forbidden`.

### 1. Generate a secret

Any sufficiently random string works. ~32 bytes is plenty:

    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

Example output:

    s9vK2_oQ8nE7yT4xR1pL6zB0mC3jH5aF8wD2qV-uIyc

### 2. Put it in your `.env`

    RECOVERY_API_KEY=s9vK2_oQ8nE7yT4xR1pL6zB0mC3jH5aF8wD2qV-uIyc

Make sure `env_file: - .env` is set on the chasemapper service in your
`docker-compose.yml`, then `docker compose up -d`.

### 3. Inject the header at Cloudflare

This is what closes the loop — Cloudflare adds the header so legitimate
browser requests pass through, while raw requests from the public
internet (no header) get rejected.

1. Cloudflare dashboard → your domain → **Rules** → **Transform Rules**
   → **Modify Request Header** → **Create rule**
2. **When incoming requests match**: pick the hostname / path scope you
   want covered. A simple option is "Hostname equals chasemapper.example.com".
3. **Then…** → **Set static** →
   - Header name: `X-Recovery-Key`
   - Value: paste the same secret you put in `.env`
4. Deploy.

Cloudflare now stamps every request to that hostname with the header
before it reaches your origin.

### 4. Verify

From a machine that bypasses Cloudflare (e.g. SSH into the host and curl
localhost), the endpoints should reject unkeyed requests:

    curl -i http://localhost:5001/airspace/status
    # → HTTP/1.1 403 Forbidden
    # → {"error":"forbidden"}

    curl -i -H "X-Recovery-Key: s9vK2_..." http://localhost:5001/airspace/status
    # → HTTP/1.1 200 OK
    # → {"loaded":...}

From a real browser through Cloudflare, the airspace toggle in the UI
should just work — if it 403s, the Transform Rule isn't matching that
request path.

### Rotating the key

Update both places at once:

1. New value in `.env` → `docker compose up -d` (the container restarts
   and re-reads the env)
2. Update the Cloudflare Transform Rule with the same new value

There's no key list or revocation — the server only knows one value at
a time.