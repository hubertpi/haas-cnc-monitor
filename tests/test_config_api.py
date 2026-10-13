import json
import time
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.main import create_app
from app.models import MachineInput, Endpoint
from app.store import Store


@pytest.mark.parametrize("host", ["http://1.2.3.4", "999.2.3.4", "x;shutdown", "", "224.0.0.1", "0.0.0.0", "-bad", "a/b"])
def test_invalid_addresses(host):
    with pytest.raises(ValidationError):
        Endpoint(host=host)


@pytest.mark.parametrize("host", ["192.168.10.20", "cnc-floor.example", "::1", "2001:db8::2"])
def test_valid_addresses(host):
    assert Endpoint(host=host).host == host


def test_store_corruption_does_not_reset_identities(tmp_path):
    path = tmp_path / "machines.json"
    path.write_text('{"version":42,"machines":[]}')
    with pytest.raises(ValueError): Store(path)
    assert '42' in path.read_text()


def test_api_onboarding_identity_and_restart(tmp_path):
    app = create_app(tmp_path, {"mode": "simulation"}, development=True)
    with TestClient(app) as client:
        csrf = client.get("/api/status").json()["csrf"]
        headers = {"x-monitor-csrf": csrf}
        endpoint = {"host": "192.0.2.20", "port": 8082, "timeout": 3}
        value = {**endpoint, "name": "Lathe A", "interval": 1, "enabled": True}
        assert client.post("/api/machines", json=value).status_code == 403
        assert client.post("/api/machines", json=value, headers=headers).status_code == 409
        tested = client.post("/api/test", json=endpoint, headers=headers).json()
        assert tested["simulation"] is True and tested["controller"]["series"] == "NGC"
        assert "192.0.2.20" in tested["suggested_name"]
        headers["x-connection-test"] = tested["test_token"]
        assert client.post("/api/machines", json={**value, "host": "192.0.2.21"}, headers=headers).status_code == 409
        response = client.post("/api/machines", json=value, headers=headers)
        assert response.status_code == 201, response.text
        machine = response.json(); identity = machine["id"]
        time.sleep(.1)
        item = client.get("/api/status").json()["machines"][0]
        assert item["online"] and item["last_success"] and len(item["signals"]) >= 10
        journal = app.state.publisher.journal
        config_topic = next(t for t in journal["topics"] if t.startswith("homeassistant/"))
        config = json.loads(journal["topics"][config_topic])
        uniques = {c["unique_id"] for c in config["components"].values()}
        assert config["device"]["identifiers"] == [identity]
        assert {c["platform"] for c in config["components"].values()} == {"sensor", "binary_sensor", "number", "switch"}
        assert all("/monitor/set/" in c["command_topic"] for c in config["components"].values() if "command_topic" in c)
        rename = {**value, "name": "Renamed lathe", "revision": 1}
        assert client.put("/api/machines/" + identity, json=rename, headers=headers).status_code == 200
        assert client.put("/api/machines/" + identity, json=rename, headers=headers).status_code == 409
        time.sleep(.1)
        new_config = json.loads(app.state.publisher.journal["topics"][config_topic])
        assert uniques == {c["unique_id"] for c in new_config["components"].values()}
        assert new_config["device"]["name"] == "Renamed lathe"
    with TestClient(create_app(tmp_path, {"mode": "simulation"}, development=True)) as client:
        status = client.get("/api/status").json()
        assert status["machines"][0]["id"] == identity
        assert status["machines"][0]["name"] == "Renamed lathe"
        assert client.delete("/api/machines/" + identity, headers={"x-monitor-csrf": status["csrf"]}).status_code == 200
        assert client.get("/api/status").json()["machines"] == []
    journal = json.loads((tmp_path / "mqtt-journal.json").read_text())
    assert journal["topics"][config_topic] == ""
    assert all(payload == "" for topic, payload in journal["topics"].items() if "/machine/" in topic)


def test_ingress_peer_and_base_path(tmp_path):
    with TestClient(create_app(tmp_path, {}, development=False)) as client:
        assert client.get("/", headers={"x-forwarded-for": "172.30.32.2"}).status_code == 403
    with TestClient(create_app(tmp_path, {}, development=True)) as client:
        response = client.get("/", headers={"x-ingress-path": "/api/hassio_ingress/test-token"})
        assert '<base href="/api/hassio_ingress/test-token/">' in response.text
        assert client.get("/", headers={"x-ingress-path": 'https://evil.example/'}).status_code == 400
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/other.js").status_code == 404


def test_hardware_mode_fails_closed(tmp_path):
    with TestClient(create_app(tmp_path, {"mode": "haas", "hardware_testing_enabled": False}, development=True)) as client:
        status = client.get("/api/status").json()
        assert "Hardware connections are disabled" in status["startup_error"]
        assert client.post("/api/test", json={"host": "192.0.2.1"}, headers={"x-monitor-csrf": status["csrf"]}).status_code == 409


def test_stale_data_marks_machine_offline_independently(tmp_path):
    app = create_app(tmp_path, {"mode": "simulation"}, development=True)
    with TestClient(app) as client:
        headers = {"x-monitor-csrf": client.get("/api/status").json()["csrf"]}
        endpoint = {"host": "192.0.2.30", "port": 8082, "timeout": 3}
        headers["x-connection-test"] = client.post("/api/test", json=endpoint, headers=headers).json()["test_token"]
        identity = client.post("/api/machines", json={**endpoint, "name": "Stale test", "interval": 60, "fast_interval": 60.0}, headers=headers).json()["id"]
        deadline = time.monotonic() + 2
        while not app.state.manager.status[identity]["online"] and time.monotonic() < deadline:
            time.sleep(.02)
        assert app.state.manager.status[identity]["online"]
        # Advance observed age, without waiting minutes or contacting hardware.
        app.state.manager.status[identity]["last_success"] = "2000-01-01T00:00:00+00:00"
        deadline = time.monotonic() + 2
        while app.state.manager.status[identity]["online"] and time.monotonic() < deadline:
            time.sleep(.02)
        status = client.get("/api/status").json()["machines"][0]
        assert not status["online"] and "stale" in status["error"]
        pub = app.state.publisher
        assert pub.journal["topics"][pub.machine_root(identity) + "/availability"] == "offline"


def test_ui_preferences_asset_is_served_without_expanding_file_access(tmp_path):
    with TestClient(create_app(tmp_path, {'mode':'simulation'}, development=True)) as client:
        page = client.get('/')
        assert 'assets/preferences.js' in page.text
        assert 'id="appearance"' in page.text and 'id="machine-sort"' in page.text
        asset = client.get('/assets/preferences.js')
        assert asset.status_code == 200 and 'MonitorUI' in asset.text
        assert client.get('/assets/worker/Program.cs').status_code == 404
        assert client.get('/assets/unlisted.js').status_code == 404


def test_shared_title_validation_csrf_revision_and_restart(tmp_path):
    with TestClient(create_app(tmp_path, {'mode':'simulation'}, development=True)) as client:
        initial = client.get('/api/status').json()
        assert initial['ui_settings'] == {'title':'Your machine floor', 'revision':0}
        payload = {'title':'  Production floor  ', 'revision':0}
        assert client.put('/api/ui-settings', json=payload).status_code == 403
        headers = {'x-monitor-csrf':initial['csrf']}
        assert client.put('/api/ui-settings', json=payload, headers=headers).json() == {'title':'Production floor', 'revision':1}
        assert client.put('/api/ui-settings', json={'title':'Other','revision':0}, headers=headers).status_code == 409
        for title in ['   ', 'x'*81, 'A\nB']:
            assert client.put('/api/ui-settings', json={'title':title,'revision':1}, headers=headers).status_code == 422
        assert client.get('/api/status').json()['machines'] == []
        assert not (tmp_path/'machines.json').exists()
    with TestClient(create_app(tmp_path, {'mode':'simulation'}, development=True)) as client:
        assert client.get('/api/status').json()['ui_settings'] == {'title':'Production floor','revision':1}


def test_title_edit_does_not_restart_existing_collector(tmp_path):
    with TestClient(create_app(tmp_path, {'mode':'simulation'}, development=True)) as client:
        headers = {'x-monitor-csrf':client.get('/api/status').json()['csrf']}
        endpoint = {'host':'192.0.2.33','port':8082,'timeout':3}
        headers['x-connection-test'] = client.post('/api/test', json=endpoint, headers=headers).json()['test_token']
        machine = client.post('/api/machines', json={**endpoint,'name':'Lathe'}, headers=headers).json()
        task = client.app.state.manager.tasks[machine['id']]
        stored = (tmp_path/'machines.json').read_bytes()
        client.put('/api/ui-settings',json={'title':'<b>Machine shop</b>','revision':0},headers=headers).raise_for_status()
        assert client.app.state.manager.tasks[machine['id']] is task
        assert (tmp_path/'machines.json').read_bytes() == stored
