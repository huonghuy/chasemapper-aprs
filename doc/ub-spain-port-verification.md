# UB-Spain reusable improvements: verification

Validated on 2026-09-18. Deployment remains separate.

## Source and scope

Merged Project Horus through `956dda6`. Retained the fork's single-platform
`linux/amd64` workflow, registry permissions, and non-publishing PR builds;
updated the checkout Action to the upstream version and skipped PR registry login.
The Docker base and runtime were unchanged.

Ported flight export, profile SPOT feeds, central map panes, lazy/retryable KML
loading, log flushing, fractional ascent averaging, and cache/parcel performance
changes. FAA pagination, atomic cache files, recovery authentication, Maryland
boundaries and owner/address popups, miles, and geofence stale-response handling
remain. No Spanish providers, mission defaults, or eclipse assets were copied.

## Automated checks

- `python3 -m pytest -q`: **67 passed** (including the original 14 tests).
- `node --test tests/test_frontend.cjs`: **2 passed**.
- Python byte-compilation and JavaScript syntax checks passed.
- `git diff --check` passed.

Coverage includes invalid/fractional averaging, duplicate timestamps, predictor
flag and scheduler recovery, bounded parallel cache refresh, older-refresh
publication rejection, failed-cache publication fallback, warm startup, FAA
pagination, export authentication/coordinates/symlink containment/style IDs/current
context, SPOT inheritance/disable/rapid switches/retired callbacks/two-client
removal, stale export selections, and client map-layer removal.

## Browser

Served the repository on loopback and opened
`/tests/browser_recovery.html` in Safari with the bundled Leaflet library.
All nine harness checks passed: initial parcels, superseded response rejection,
lazy popup construction, movement threshold, radius refetch, failed-fetch retry,
hidden KML lazy loading, failed-load state, and KML retry.

Manually clicked overlapping polygons: the parcel popup opened, including escaped
owner text and Maps links. Used the real geofence controls to place three vertices
through overlapping polygons. Cancel restored normal parcel popup interaction.
The harness uses controlled fetch/KML responses; it does not validate live FAA,
Maryland, or SPOT service availability.

Reproduce with `python3 -m http.server 5057 --bind 127.0.0.1`, then open
`http://127.0.0.1:5057/tests/browser_recovery.html`.

## Container

Built `chasemapper:ub-port-verify` with `docker build --platform linux/amd64`.
Final tested local image index digest:

`sha256:b7edb6c94e3d9922c1c7f164ede37bd463c964e69cce2aa05aea737cb3ecc121`

Validated application imports, eccodes/cfgrib/cusfpredict imports and `./pred --help`.
Then ran the real native predictor with synthetic constant-wind model files,
telemetry ingestion, active logger batch flushing, recovery-authenticated KML
export, persisted atomic cache reads, and FAA/TFR cache headers. All passed with
container networking disabled and the normal non-root runtime user.

Reproduce the smoke test (use a dedicated writable temporary directory):

```sh
mkdir -p /tmp/chasemapper-ub-port-smoke
docker run --rm --platform linux/amd64 --network none \
  --mount type=bind,source=/tmp/chasemapper-ub-port-smoke,target=/smoke \
  --entrypoint python chasemapper:ub-port-verify tests/container_smoke.py
```

The smoke test leaves synthetic logs, model data, cache, and `smoke.kml` in that
directory. It exercises prediction mechanics, not the accuracy or availability
of live forecast data. HTTP routes are exercised through Flask's test client
inside the container; no production listener or service was deployed.

## Rollback reference

The existing local `ghcr.io/huonghuy/chasemapper-aprs:master` image was retained:

`ghcr.io/huonghuy/chasemapper-aprs@sha256:5cfb5bfad8217517089c15350b7fddb0e3faf734cc37a26f03e2144a569dafd4`

This records the local pre-build image, not a verified digest of the running
Proxmox deployment. No existing image was retagged or removed. Confirm the actual
deployment digest when deployment is scheduled.
