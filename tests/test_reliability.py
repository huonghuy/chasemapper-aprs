from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from chasemapper.geometry import GenericTrack
from chasemapper import airspace_cache as cache


@pytest.mark.parametrize('window,expected', [(2, 6), (3, 5), (3.5, 4.4), (10, 4)])
def test_ascent_windows(window, expected):
    track = GenericTrack(ascent_averaging=window)
    start = datetime.now(timezone.utc)
    track.track_history = [[start + timedelta(seconds=i), 0, 0, alt] for i, alt in enumerate([0, 2, 6, 12])]
    assert track.calculate_ascent_rate() == pytest.approx(expected)


@pytest.mark.parametrize('window', [0, 1.9, float('nan'), float('inf'), -float('inf')])
def test_invalid_ascent_windows(window):
    with pytest.raises(ValueError):
        GenericTrack(ascent_averaging=window)


def test_duplicate_timestamps_have_zero_rate():
    track = GenericTrack(ascent_averaging=2.5)
    track.track_history = [[datetime(2026, 1, 1), 0, 0, alt] for alt in [0, 2, 6]]
    assert track.calculate_ascent_rate() == 0


def test_prediction_exception_releases_busy_flag(monkeypatch):
    import horusmapper as server
    monkeypatch.setattr(server, 'chasemapper_config', {'pred_enabled': True, 'offline_predictions': False})
    monkeypatch.setattr(server, 'predictor', 'Tawhiri')
    monkeypatch.setattr(server, 'predictor_semaphore', False)
    monkeypatch.setattr(server, 'current_payload_tracks', {'TEST': object()})
    with pytest.raises(Exception):
        server.run_prediction()
    assert not server.predictor_semaphore
    monkeypatch.setattr(server, 'current_payload_tracks', {})
    server.run_prediction()
    assert not server.predictor_semaphore


def test_older_refresh_cannot_replace_newer_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, '_LAYER_PATHS', {'tfr': str(tmp_path / 'tfr.geojson')})
    monkeypatch.setattr(cache, '_meta_cache', {})
    old_started, finish_old = threading.Event(), threading.Event()
    def old():
        old_started.set()
        finish_old.wait(2)
        cache._write_layer('tfr', {'type': 'FeatureCollection', 'features': []}, 10)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old)
        assert old_started.wait(2)
        cache._write_layer('tfr', {'type': 'FeatureCollection', 'features': [{'new': True}]}, 20)
        finish_old.set()
        future.result()
    assert cache._read_layer('tfr')['fetched_at'] == 20
    assert cache.get_layer_path('tfr') == str(tmp_path / 'tfr.geojson')
    assert cache.get_layer_path('../tfr') is None


def test_startup_hydrates_missing_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, 'CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(cache, '_LAYER_PATHS', {layer: str(tmp_path / (layer + '.geojson')) for layer in cache.LAYERS})
    monkeypatch.setattr(cache, '_meta_cache', {})
    monkeypatch.setattr(cache, '_started', False)
    cache._write_layer('tfr', {'type': 'FeatureCollection', 'features': []}, 20)
    rounds = []
    monkeypatch.setattr(cache, '_refresh_layers', lambda layers: rounds.append(layers))
    monkeypatch.setattr(cache.threading.Thread, 'start', lambda self: None)
    cache.start_background_refresh()
    cache.start_background_refresh()
    assert rounds == [[layer for layer in cache.LAYERS if layer != 'tfr']]


def test_predictor_thread_continues_after_exception(monkeypatch):
    import horusmapper as server
    monkeypatch.setattr(server, 'predictor_thread_running', True)
    monkeypatch.setattr(server, 'chasemapper_config', {'pred_update_rate': 0})
    calls = []
    def predict():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('temporary failure')
        server.predictor_thread_running = False
    monkeypatch.setattr(server, 'run_prediction', predict)
    server.predictorThread()
    assert len(calls) == 2


@pytest.mark.parametrize('value,expected', [('3.5', 3.5), ('nan', 10), ('inf', 10), ('1', 10)])
def test_config_validates_averaging_and_resolves_overlays(tmp_path, monkeypatch, value, expected):
    from configparser import RawConfigParser
    from chasemapper.config import parse_config_file, _INSTALL_DIR
    from pathlib import Path
    cfg = RawConfigParser()
    cfg.read('horusmapper.cfg.example')
    cfg.set('offline_maps', 'tile_server_enabled', 'false')
    cfg.set('predictor', 'ascent_rate_averaging', value)
    if not cfg.has_section('kml_overlays'):
        cfg.add_section('kml_overlays')
    cfg.set('kml_overlays', 'overlay_count', '1')
    cfg.set('kml_overlays', 'overlay_1_name', 'Test')
    cfg.set('kml_overlays', 'overlay_1_path', 'overlays/test.kml')
    path = tmp_path / 'test.cfg'
    with path.open('w') as stream:
        cfg.write(stream)
    monkeypatch.chdir(tmp_path)
    parsed = parse_config_file(str(path))
    assert parsed['ascent_rate_averaging'] == expected
    assert parsed['kml_overlays'][0]['path'] == str(Path(_INSTALL_DIR) / 'overlays/test.kml')


def test_logger_batch_is_visible_before_close(tmp_path, monkeypatch):
    import queue
    from chasemapper import logger as module
    logger = module.ChaseLogger.__new__(module.ChaseLogger)
    logger.input_queue = queue.Queue()
    logger.input_queue.put({'test': 'flushed'})
    logger.file_lock = threading.Lock()
    logger.input_processing_running = True
    path = tmp_path / 'batch.log'
    logger.f = path.open('w')
    def after_batch(_):
        assert 'flushed' in path.read_text()
        logger.input_processing_running = False
    monkeypatch.setattr(module.time, 'sleep', after_batch)
    try:
        logger.process_queue()
    finally:
        logger.f.close()


def test_parallel_refresh_is_bounded_and_reports_failure(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3)
    def refresh(layer):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return layer != 'tfr'
    monkeypatch.setattr(cache, '_try_refresh', refresh)
    results = cache._refresh_layers(cache.LAYERS)
    assert peak == 3
    assert results['tfr'] is False
    assert all(results[layer] for layer in cache.LAYERS if layer != 'tfr')
