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
