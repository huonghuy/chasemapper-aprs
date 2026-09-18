import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import horusmapper as server
from chasemapper.config import parse_config_file


@pytest.fixture
def configured(monkeypatch):
    profiles = {
        'A': {'name': 'A', 'spot_feeds': [('A-SPOT', 'A_FEED')]},
        'B': {'name': 'B', 'spot_feeds': [('B-SPOT', 'B_FEED')]},
        'Legacy': {'name': 'Legacy'},
        'Off': {'name': 'Off', 'spot_feeds': []},
    }
    monkeypatch.setattr(server, 'chasemapper_config', {
        'profiles': profiles, 'selected_profile': 'A', 'spot_feeds': [('GLOBAL', 'FEED')],
    })
    monkeypatch.setattr(server, 'current_payloads', {'A-SPOT': {}, 'BALLOON': {}})
    monkeypatch.setattr(server, 'current_payload_tracks', {'A-SPOT': object(), 'BALLOON': object()})
    monkeypatch.setattr(server, '_clear_launch_preview', lambda *a: None)
    monkeypatch.setattr(server, '_aprsis_state', lambda: {})
    monkeypatch.setattr(server, '_spot_session', 1)
    return profiles


def test_fallback_and_explicit_empty(configured):
    assert server._spot_feeds_for_profile(configured['Legacy']) == [('GLOBAL', 'FEED')]
    assert server._spot_feeds_for_profile(configured['Off']) == []
    assert server._profile_spot_callsigns('A') == ['A-SPOT']
    assert server._all_spot_callsigns() == {'A-SPOT', 'B-SPOT', 'GLOBAL'}


def test_config_profiles_inherit_or_disable(tmp_path):
    text = Path('horusmapper.cfg.example').read_text()
    from configparser import RawConfigParser
    cfg = RawConfigParser()
    cfg.read_string(text)
    cfg.set('offline_maps', 'tile_server_enabled', 'false')
    cfg.set('spot', 'spot_enabled', 'true')
    cfg.set('spot', 'spot_feeds', 'GLOBAL:GLOBAL_FEED')
    cfg.set('profile_2', 'spot_feeds', '')
    path = tmp_path / 'test.cfg'
    with path.open('w') as stream:
        cfg.write(stream)
    parsed = parse_config_file(str(path))
    assert parsed['spot_feeds'] == [('GLOBAL', 'GLOBAL_FEED')]
    assert parsed['profiles'][cfg.get('profile_1', 'profile_name')]['spot_feeds'] == [('GLOBAL', 'GLOBAL_FEED')]
    assert parsed['profiles'][cfg.get('profile_2', 'profile_name')]['spot_feeds'] == []


def test_retired_callbacks_and_broadcast_removal(configured, monkeypatch):
    received = []
    monkeypatch.setattr(server, 'udp_listener_summary_callback', received.append)
    monkeypatch.setattr(server, 'start_listeners', lambda profile: None)
    old = server._spot_callback(server._spot_session)
    clients = [server.socketio.test_client(server.app, namespace='/chasemapper') for _ in range(2)]
    try:
        old({'callsign': 'A-SPOT'})
        server.profile_change('B')
        old({'callsign': 'A-SPOT'})
        server.profile_change('A')  # Same callsign again must not revive the old session.
        old({'callsign': 'A-SPOT'})
        assert len(received) == 1
        assert set(server.current_payloads) == {'BALLOON'}
        assert set(server.current_payload_tracks) == {'BALLOON'}
        for client in clients:
            events = client.get_received('/chasemapper')
            assert any(e['name'] == 'payload_removed' and e['args'][0]['callsigns'] == ['A-SPOT'] for e in events)
    finally:
        for client in clients:
            client.disconnect(namespace='/chasemapper')


def test_rapid_profile_changes_are_serial(configured, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    started = []
    def start(profile):
        started.append(profile['name'])
        if profile['name'] == 'B':
            entered.set()
            assert release.wait(2)
    monkeypatch.setattr(server, 'start_listeners', start)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(server.profile_change, 'B')
        assert entered.wait(2)
        second = pool.submit(server.profile_change, 'A')
        release.set()
        first.result()
        second.result()
    assert started == ['B', 'A']
    assert server.chasemapper_config['selected_profile'] == 'A'


def test_spot_is_excluded_before_prediction_reads_track(configured, monkeypatch):
    server.chasemapper_config.update(pred_enabled=True, offline_predictions=False)
    monkeypatch.setattr(server, 'predictor', 'Tawhiri')
    monkeypatch.setattr(server, 'predictor_semaphore', False)
    monkeypatch.setattr(server, 'current_payload_tracks', {'B-SPOT': object(), 'GLOBAL': object()})
    server.run_prediction()  # Objects intentionally have no prediction methods.
    assert not server.predictor_semaphore
