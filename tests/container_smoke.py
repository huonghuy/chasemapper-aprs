"""Offline amd64 smoke test. Run from /opt/chasemapper with persistent /smoke mount.

Uses synthetic constant winds, never live forecasts or telemetry services.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import time
from configparser import RawConfigParser
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import numpy as np
import eccodes
import cfgrib
from cusfpredict.gfs import wind_dict_to_cusf
from cusfpredict.predict import Predictor
import horusmapper as server
from chasemapper.config import parse_config_file
from chasemapper.logger import ChaseLogger
from chasemapper import airspace_cache as cache

root = Path('/smoke')
for sub in ['gfs', 'logs', 'airspace']:
    (root / sub).mkdir(parents=True, exist_ok=True)
now = datetime.now(timezone.utc).replace(microsecond=0)
for offset in [-3600, 3600]:
    wind = dict(lat_scale=[38., 39., 40.], lon_scale=[283., 284., 285.],
                lat_centre=39., lon_centre=284., lat_radius=1., lon_radius=1.,
                valid_time=int(now.timestamp()) + offset)
    for pressure, altitude in [(1000, 0), (900, 1000), (700, 3000), (500, 5000), (250, 10000)]:
        wind[pressure] = {'HGT': np.full((3, 3), altitude), 'UGRD': np.full((3, 3), 3.), 'VGRD': np.full((3, 3), 1.)}
    wind_dict_to_cusf(wind, output_dir=str(root / 'gfs'))

cfg = RawConfigParser()
cfg.read('horusmapper.cfg.example')
cfg.set('offline_maps', 'tile_server_enabled', 'false')
cfg.set('habitat', 'habitat_upload_enabled', 'false')
cfg.set('spot', 'spot_enabled', 'false')
with (root / 'smoke.cfg').open('w') as f:
    cfg.write(f)
server.chasemapper_config = parse_config_file(str(root / 'smoke.cfg'))
server.chasemapper_config.update(pred_enabled=True, offline_predictions=True, pred_burst=1500., pred_desc_rate=5., pred_abort=False,
                                selected_profile='Smoke', profiles={'Smoke': {'aprsis_balloon_callsigns': ['SMOKE']}})
server.predictor = Predictor(bin_path='./pred', gfs_path=str(root / 'gfs'))
server.predictor_model_end = now + timedelta(hours=8)
server.RECOVERY_API_KEY = 'smoke-test'
server.chase_logger = ChaseLogger(filename=str(root / 'logs' / 'smoke.log'))
try:
    # Telemetry playback through the real ingestion, track and logger paths.
    for index, altitude in enumerate([990, 1000, 1010]):
        server.handle_new_payload_position(dict(callsign='SMOKE', lat=39., lon=-76., alt=altitude,
                                               time_dt=now-timedelta(seconds=10-index*5), comment='Synthetic smoke'))
    server.run_prediction()
    assert not server.predictor_semaphore
    assert len(server.current_payloads['SMOKE']['pred_path']) > 2
    assert server.current_payloads['SMOKE']['pred_landing'][2] <= 1
    # Batch flushing makes export available without closing the running logger.
    deadline = time.monotonic() + 12
    while 'PREDICTION' not in (root / 'logs' / 'smoke.log').read_text():
        assert time.monotonic() < deadline, 'logger did not flush'
        time.sleep(.1)
    client = server.app.test_client()
    headers = {'X-Recovery-Key': 'smoke-test'}
    assert client.get('/export/kml?log=smoke.log').status_code == 403
    response = client.get('/export/kml?log=smoke.log', headers=headers)
    assert response.status_code == 200
    ElementTree.fromstring(response.data)
    assert b'First recorded prediction' in response.data
    (root / 'smoke.kml').write_bytes(response.data)
    cache.CACHE_DIR = str(root / 'airspace')
    cache._LAYER_PATHS = {layer: str(root / 'airspace' / (layer + '.geojson')) for layer in cache.LAYERS}
    for layer in cache.LAYERS:
        cache._write_layer(layer, {'type': 'FeatureCollection', 'features': []}, time.time())
    cache._meta_cache.clear()  # Read persisted atomic cache as after a restart.
    for layer in cache.LAYERS:
        response = client.get('/airspace/' + layer, headers=headers)
        assert response.status_code == 200
        assert response.json['feature_count'] == 0
        assert response.headers['Cache-Control'] == ('public, max-age=60' if layer == 'tfr' else 'public, max-age=300')
    print('PASS: imports, native prediction with synthetic winds, telemetry playback, live log flushing, authenticated KML download, persistent FAA cache and headers')
finally:
    server.chase_logger.close()
    server.chase_logger.log_process_thread.join(timeout=6)
