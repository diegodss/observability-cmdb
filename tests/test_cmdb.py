"""Tests for definitions, plan/apply (against a fake Assets API) and enrichment."""

import pytest
from fastapi.testclient import TestClient

from cmdb import model, provision
from cmdb.assets import AssetsClient
from cmdb.config import Config
from cmdb.directory import JiraDirectory, MockDirectory
from cmdb.enrich import enrich
from cmdb.server import create_app
from fake_assets import create_fake

EDW = "EDW"
STUDENT = "ETL - DXMULTI-0001 : AppEvents from SMS to EDW - Student"
EDW_CONNECTOR = "CONN - Data Warehouse (EDW) DB"


@pytest.fixture
def schemas():
    return model.load_schemas()


@pytest.fixture
def fake():
    return create_fake()


@pytest.fixture
def client(fake):
    config = Config(backend="jira", jira_site="", jira_email="test@example.com", jira_api_token="t",
                    workspace_id="", api_base="http://fake")
    return AssetsClient(config, http=TestClient(fake, base_url="http://fake"))


def test_definitions_are_consistent(schemas):
    assert [s.name for s in schemas] == ["Applications", "Connectors", "Integrations"]
    assert model.validate(schemas) == []


def test_plan_on_empty_jira_lists_everything_and_changes_nothing(client, fake, schemas):
    result = provision.run(client, schemas, apply=False)
    assert result.count("create") > 0 and result.count("update") == 0
    assert not fake.state.data["schemas"]


def test_apply_builds_everything_and_is_idempotent(client, fake, schemas):
    first = provision.run(client, schemas, apply=True)
    assert first.count("drift") == 0
    data = fake.state.data
    assert sorted(s["name"] for s in data["schemas"].values()) == ["Applications", "Connectors", "Integrations"]
    assert len(data["objects"]) == 8  # 2 applications, 2 connectors, 4 integrations

    second = provision.run(client, schemas, apply=False)
    assert (second.count("create"), second.count("update")) == (0, 0), \
        [a for a in second.actions if a.verb in ("create", "update")]


def test_apply_updates_only_changed_values(client, schemas, monkeypatch):
    provision.run(client, schemas, apply=True)
    original = provision.load_objects

    def edited(object_type, *args):
        rows = original(object_type, *args)
        if object_type.name == "Application":
            rows = [dict(r, **{"Resolver Group": "Data Platform"}) if r["Name"] == EDW else r for r in rows]
        return rows

    monkeypatch.setattr(provision, "load_objects", edited)
    result = provision.run(client, schemas, apply=True)
    updates = [a for a in result.actions if a.verb == "update"]
    assert len(updates) == 1 and "EDW" in updates[0].target and updates[0].detail == "Resolver Group"
    assert enrich(JiraDirectory(schemas, client), EDW)["resolver_group"] == "Data Platform"


def _comparable(result: dict) -> dict:
    """Drop backend-specific fields (object keys, timing) before comparing."""
    result = {k: v for k, v in result.items() if k != "enrichment"}
    for section in ("ci", "root_ci"):
        if section in result:
            result[section] = {k: v for k, v in result[section].items() if k != "key"}
    for side in ("source_system", "target_system"):
        if result.get("details", {}).get(side):
            result["details"][side] = {k: v for k, v in result["details"][side].items() if k != "key"}
    if "details" in result and "connector_cis" in result["details"]:
        result["details"]["connector_cis"] = [{k: v for k, v in c.items() if k != "key"} for c in result["details"]["connector_cis"]]
    return result


@pytest.mark.parametrize("name,failed_side", [(EDW, None), (STUDENT, None), (STUDENT, "target"),
                                              (EDW_CONNECTOR, None), ("Unknown CI", None)])
def test_jira_and_mock_backends_agree(client, schemas, name, failed_side):
    provision.run(client, schemas, apply=True)
    jira = enrich(JiraDirectory(schemas, client), name, failed_side)
    mock = enrich(MockDirectory(schemas), name, failed_side)
    assert _comparable(jira) == _comparable(mock)


def test_edw_enrichment(schemas):
    result = enrich(MockDirectory(schemas), EDW)
    assert result["root_ci"]["name"] == EDW and result["resolver_group"] == "Data Squad"
    assert result["informed_groups"] == ["Current Students"]
    assert result["impact"]["count"] == 4
    assert {r["direction"] for r in result["impact"]["integrations"]} == {"writes to"}
    assert result["impact"]["domains"] == ["Current Students / Student Enrolment"]


def test_integration_enrichment(schemas):
    own = enrich(MockDirectory(schemas), STUDENT)
    assert own["root_ci"]["name"] == STUDENT and own["resolver_group"] == "Current Students"
    assert own["informed_groups"] == []
    assert own["details"]["target_system"]["resolver_group"] == "Data Squad"
    assert [c["name"] for c in own["details"]["connector_cis"]] == [EDW_CONNECTOR, "CONN - Technology One Student SOAP - ADF Services"]

    target_side = enrich(MockDirectory(schemas), STUDENT, failed_side="target")
    assert target_side["root_ci"]["name"] == EDW and target_side["resolver_group"] == "Data Squad"
    assert target_side["informed_groups"] == ["Current Students"] and target_side["impact"]["count"] == 4


def test_http_service(schemas):
    directory = MockDirectory(schemas)
    http = TestClient(create_app(lambda: directory))
    assert http.get("/health").json() == {"status": "ok", "backend": "mock"}
    response = http.get("/ci/" + STUDENT, params={"failed_side": "target"})
    assert response.status_code == 200 and response.json()["resolver_group"] == "Data Squad"
    assert http.get("/ci/Nope").status_code == 404
    assert http.get("/ci/EDW", params={"failed_side": "sideways"}).status_code == 422
