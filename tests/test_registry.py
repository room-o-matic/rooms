import pytest

from roomsd import auth, db

URL = "/v1/registry/agentd"


def registration(**overrides):
    return {
        "base_url": "http://host1:8765",
        "worker_types": ["codex", "claude"],
        "profiles": ["workspace_coder"],
        "max_sessions": 4,
        "active_sessions": 1,
        **overrides,
    }


def expire(settings, instance_id):
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute(
            "update agentd_instances set expires_at = '2000-01-01T00:00:00.000Z'"
            " where instance_id = ?",
            (instance_id,),
        )
    conn.close()


def test_register_and_lookup(client, agentd1, boostie):
    r = client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1)
    assert r.status_code == 200
    inst = r.json()
    assert inst["available_sessions"] == 3
    assert inst["registered_at"] == inst["last_heartbeat_at"]

    listed = client.get(URL, headers=boostie).json()
    assert [i["instance_id"] for i in listed] == ["agentd-host1"]
    assert client.get(f"{URL}/agentd-host1", headers=boostie).status_code == 200


def test_heartbeat_keeps_registered_at(client, agentd1):
    first = client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1).json()
    second = client.put(
        f"{URL}/agentd-host1", json=registration(active_sessions=2), headers=agentd1
    ).json()
    assert second["registered_at"] == first["registered_at"]
    assert second["active_sessions"] == 2


def test_instance_can_only_manage_itself(client, agentd1, make_agent):
    make_agent("agentd-host2", "agentd")
    r = client.put(f"{URL}/agentd-host2", json=registration(), headers=agentd1)
    assert r.status_code == 403
    assert client.delete(f"{URL}/agentd-host2", headers=agentd1).status_code == 403


def test_named_agents_cannot_register(client, boostie):
    assert client.put(f"{URL}/boostie", json=registration(), headers=boostie).status_code == 403


def test_agentd_cannot_use_rooms_or_list_registry(client, agentd1):
    assert client.post("/v1/rooms", json={"name": "x"}, headers=agentd1).status_code == 403
    assert client.get("/v1/rooms", headers=agentd1).status_code == 403
    assert client.get(URL, headers=agentd1).status_code == 403


def test_agentd_can_read_own_entry(client, agentd1):
    client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1)
    assert client.get(f"{URL}/agentd-host1", headers=agentd1).status_code == 200


def test_expired_entries_hidden(client, settings, agentd1, boostie):
    client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1)
    expire(settings, "agentd-host1")
    assert client.get(URL, headers=boostie).json() == []
    assert client.get(f"{URL}/agentd-host1", headers=boostie).status_code == 404


def test_reregistering_after_expiry_resets_registered_at(client, settings, agentd1):
    client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1)
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute("update agentd_instances set registered_at = 'old'")
    conn.close()
    expire(settings, "agentd-host1")
    again = client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1).json()
    assert again["registered_at"] != "old"


def test_deregister(client, agentd1, boostie):
    client.put(f"{URL}/agentd-host1", json=registration(), headers=agentd1)
    assert client.delete(f"{URL}/agentd-host1", headers=agentd1).status_code == 204
    assert client.get(URL, headers=boostie).json() == []


def test_filters_and_capacity_ordering(client, make_agent, boostie):
    hosts = {
        "h-busy": registration(worker_types=["codex"], max_sessions=2, active_sessions=2),
        "h-roomy": registration(worker_types=["codex"], max_sessions=8, active_sessions=1),
        "h-claude": registration(worker_types=["claude"], profiles=["read_only_research"]),
    }
    for name, body in hosts.items():
        r = client.put(f"{URL}/{name}", json=body, headers=make_agent(name, "agentd"))
        assert r.status_code == 200

    def ids(**params):
        return [i["instance_id"] for i in client.get(URL, params=params, headers=boostie).json()]

    assert ids(worker_type="codex") == ["h-roomy", "h-busy"]
    assert ids(worker_type="codex", has_capacity=True) == ["h-roomy"]
    assert ids(profile="read_only_research") == ["h-claude"]
    assert ids(worker_type="fake") == []


def test_ttl_and_payload_validation(client, agentd1):
    put = lambda body: client.put(f"{URL}/agentd-host1", json=body, headers=agentd1)  # noqa: E731
    assert put(registration(ttl_seconds=10_000)).status_code == 422
    assert put(registration(worker_types=[])).status_code == 422
    assert put(registration(base_url="ftp://x")).status_code == 422


def test_name_cannot_span_scopes(settings, boostie):
    conn = db.connect(settings.db_path)
    try:
        with pytest.raises(ValueError, match="already has 'agent' tokens"):
            auth.create_token(conn, "boostie", "agentd")
    finally:
        conn.close()
