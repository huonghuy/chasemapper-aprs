from datetime import datetime, timezone
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
        'ascent_rate_averaging': 10,
    })
    monkeypatch.setattr(server, 'current_payloads', {'A-SPOT': {}, 'BALLOON': {}})
    monkeypatch.setattr(server, 'current_payload_tracks', {'A-SPOT': object(), 'BALLOON': object()})
    monkeypatch.setattr(server, '_clear_launch_preview', lambda *a: None)
    monkeypatch.setattr(server, '_aprsis_state', lambda *a: {})
    monkeypatch.setattr(server, '_client_profiles', {})
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


def _viewer(profile):
    client = server.socketio.test_client(server.app, namespace='/chasemapper')
    client.emit('profile_change', profile, namespace='/chasemapper')
    client.get_received('/chasemapper')
    return client


def _position(callsign, second=0, port=None):
    server.handle_new_payload_position({
        'callsign': callsign, 'lat': 1.0, 'lon': 2.0, 'alt': 100.0 + second,
        'time_dt': datetime(2026, 9, 18, 12, 0, second, tzinfo=timezone.utc),
    }, log_position=False, source_port=port)


def _telemetry(client):
    return [e['args'][0]['callsign'] for e in client.get_received('/chasemapper')
            if e['name'] == 'telemetry_event']


def test_retired_spot_callbacks_are_dropped(configured, monkeypatch):
    received = []
    monkeypatch.setattr(server, 'udp_listener_summary_callback', received.append)
    old = server._spot_callback(server._spot_session)
    old({'callsign': 'A-SPOT'})
    server._spot_session += 1  # What start_listeners does when it restarts.
    old({'callsign': 'A-SPOT'})
    assert len(received) == 1


def test_payloads_route_to_their_profiles(configured):
    configured['A'].update(telemetry_source_type='horus_udp', telemetry_source_port=55673)
    configured['B'].update(telemetry_source_type='horus_udp', telemetry_source_port=55672,
                           aprsis_balloon_callsigns=['W3EAX-11'])
    assert server._profiles_for_payload('A-SPOT') == ['A']
    assert server._profiles_for_payload('w3eax-11', source_port=55673) == ['B']
    assert server._profiles_for_payload('RS41', source_port=55672) == ['B']
    assert server._profiles_for_payload('RS41') == list(configured)


def test_each_viewer_only_sees_its_profile(configured, monkeypatch):
    monkeypatch.setattr(server, 'current_payloads', {})
    monkeypatch.setattr(server, 'current_payload_tracks', {})
    a, b = _viewer('A'), _viewer('B')
    try:
        _position('A-SPOT')
        _position('B-SPOT')
        assert _telemetry(a) == ['A-SPOT']
        assert _telemetry(b) == ['B-SPOT']

        # Switching one viewer leaves the other alone.
        a.emit('profile_change', 'B', namespace='/chasemapper')
        _position('B-SPOT', second=1)
        assert _telemetry(a) == ['B-SPOT']
        assert _telemetry(b) == ['B-SPOT']
        assert server.chasemapper_config['selected_profile'] == 'A'

        archive = server.app.test_client().get('/get_telemetry_archive?profile=A').get_json(force=True)
        assert set(archive) == {'A-SPOT'}
    finally:
        a.disconnect(namespace='/chasemapper')
        b.disconnect(namespace='/chasemapper')
    assert server._client_profiles == {}


def test_clear_only_clears_the_viewers_profile(configured, monkeypatch):
    monkeypatch.setattr(server, 'current_payloads', {})
    monkeypatch.setattr(server, 'current_payload_tracks', {})
    a, b = _viewer('A'), _viewer('B')
    try:
        _position('A-SPOT')
        _position('B-SPOT')
        a.get_received('/chasemapper')
        b.get_received('/chasemapper')
        a.emit('payload_data_clear', {}, namespace='/chasemapper')
        assert set(server.current_payloads) == {'B-SPOT'}
        assert not any(e['name'] == 'payload_removed' for e in b.get_received('/chasemapper'))
    finally:
        a.disconnect(namespace='/chasemapper')
        b.disconnect(namespace='/chasemapper')


def test_listeners_run_for_every_profile(monkeypatch):
    started = {'udp': [], 'aprs': []}

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.active_car_callsign = kwargs.get('active_car_callsign', '')
        def start(self):
            pass
        def close(self):
            pass

    class FakeUDP(Fake):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            started['udp'].append((kwargs['port'], kwargs['summary_callback'] is not None))

    class FakeAPRS(Fake):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            started['aprs'].append(kwargs)

    def profile(name, port, balloons):
        return {'name': name, 'telemetry_source_type': 'horus_udp', 'telemetry_source_port': port,
                'car_source_type': 'aprsis', 'car_source_port': 55672, 'online_tracker': 'sondehub',
                'aprsis_balloon_callsigns': balloons, 'aprsis_car_callsigns': ['CAR-2'],
                'aprsis_active_car_callsign': 'CAR-2'}

    monkeypatch.setattr(server, 'UDPListener', FakeUDP)
    monkeypatch.setattr(server, 'APRSISListener', FakeAPRS)
    monkeypatch.setattr(server, 'data_listeners', [])
    monkeypatch.setattr(server, 'online_uploader', None)
    monkeypatch.setattr(server, 'chasemapper_config', {
        'selected_profile': 'One', 'habitat_upload_enabled': False, 'spot_enabled': False,
        'aprsis_server': 'x', 'aprsis_port': 1, 'aprsis_login_callsign': 'N0CALL',
        'profiles': {'One': profile('One', 55673, ['B-1']), 'Two': profile('Two', 55672, ['B-2']),
                     'Three': profile('Three', 55672, ['B-3'])},
    })
    server.start_listeners()
    assert started['udp'] == [(55673, True), (55672, True)]
    assert len(started['aprs']) == 1
    assert started['aprs'][0]['balloon_callsigns'] == ['B-1', 'B-2', 'B-3']
    assert started['aprs'][0]['active_car_callsign'] == 'CAR-2'


def test_spot_is_excluded_before_prediction_reads_track(configured, monkeypatch):
    server.chasemapper_config.update(pred_enabled=True, offline_predictions=False)
    monkeypatch.setattr(server, 'predictor', 'Tawhiri')
    monkeypatch.setattr(server, 'predictor_semaphore', False)
    monkeypatch.setattr(server, 'current_payload_tracks', {'B-SPOT': object(), 'GLOBAL': object()})
    server.run_prediction()  # Objects intentionally have no prediction methods.
    assert not server.predictor_semaphore
