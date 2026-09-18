import json

import pytest
import horusmapper as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(server, 'chase_logger', None)
    monkeypatch.setattr(server, 'RECOVERY_API_KEY', '')
    monkeypatch.setattr(server, 'chasemapper_config', {'selected_profile': 'Test', 'profiles': {'Test': {}}})
    monkeypatch.setattr(server, 'geofence_store', {'profiles': {'Test': {'polygon': [[1, 1], [1, 2], [2, 2]]}}})
    monkeypatch.setattr(server, 'kml_overlay_settings', {})
    (tmp_path / 'flight.log').write_text(json.dumps({'log_type': 'BALLOON TELEMETRY', 'callsign': 'TEST', 'lat': 1, 'lon': 2, 'alt': 100}) + '\n')
    return server.app.test_client()


@pytest.mark.parametrize('route', ['flights', 'payloads', 'kml'])
def test_all_export_routes_require_auth(client, monkeypatch, route):
    monkeypatch.setattr(server, 'RECOVERY_API_KEY', 'test-key')
    assert client.get('/export/' + route).status_code == 403
    assert client.get('/export/' + route, headers={'X-Recovery-Key': 'test-key'}).status_code == 200


def test_resolved_path_containment(client, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.log')
    outside.write_text('private')
    (tmp_path / 'escape.log').symlink_to(outside)
    assert client.get('/export/kml?log=escape.log').status_code == 404
    assert client.get('/export/kml?log=../outside.log').status_code == 404
    assert [x['name'] for x in client.get('/export/flights').json['logs']] == ['flight.log']


def test_context_is_opt_in_and_labelled_current(client):
    plain = client.get('/export/kml').text
    assert 'Current geofence' not in plain
    context = client.get('/export/kml?include_geofence=1').text
    assert 'Current geofence - Test' in context
    assert 'historical configuration was not recorded' in context
