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
PACKAGE = "DXMULTI-0001"


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
    assert len(data["objects"]) == 9  # 2 applications, 2 connectors, 1 package, 4 integrations

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
        result["details"]["connector_cis"] = [{k: v for k, v in c.items() if k != "key"} if isinstance(c, dict) else c
                                              for c in result["details"]["connector_cis"]]
    return result


@pytest.mark.parametrize("name,failed_side", [(EDW, None), (STUDENT, None), (STUDENT, "target"),
                                              (EDW_CONNECTOR, None), (PACKAGE, None), ("Unknown CI", None)])
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
    assert http.get("/health").json() == {"status": "ok", "backend": "mock", "auth": False, "cache_seconds": 0}
    response = http.get("/ci/" + STUDENT, params={"failed_side": "target"})
    assert response.status_code == 200 and response.json()["resolver_group"] == "Data Squad"
    assert http.get("/ci/Nope").status_code == 404
    assert http.get("/ci/EDW", params={"failed_side": "sideways"}).status_code == 422


# ---------------------------------------------------------------- prerequisites and apply gating

def test_required_groups_come_from_the_data(schemas):
    assert provision.required_groups(schemas) == {"Current Students", "Data Squad", "Integration Squad"}


def _args(command, **overrides):
    import argparse
    values = {"command": command, "verbose": False, "skip_group_check": False, "yes": False}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def cli(client, monkeypatch):
    from cmdb import __main__ as cli_module
    monkeypatch.setattr(cli_module, "_client", lambda config: client)
    return cli_module


def test_missing_group_blocks_apply_before_any_change(cli, client, fake, schemas, monkeypatch, capsys):
    monkeypatch.setattr(client, "missing_groups", lambda groups: ["Data Squad"])
    assert cli.cmd_provision(_args("apply", yes=True), schemas, client.config) == 1
    out = capsys.readouterr().out
    assert "✗ Jira group 'Data Squad'" in out and "Nothing was changed" in out
    assert not fake.state.data["schemas"]


def test_unverifiable_groups_block_apply_unless_skipped(cli, client, fake, schemas, capsys):
    # The fake has no Jira group API, so the group check can't be made.
    assert cli.cmd_provision(_args("apply", yes=True), schemas, client.config) == 1
    assert not fake.state.data["schemas"]
    assert cli.cmd_provision(_args("apply", yes=True, skip_group_check=True), schemas, client.config) == 0
    assert len(fake.state.data["objects"]) == 9


def test_apply_without_terminal_or_yes_does_not_change_anything(cli, client, fake, schemas, monkeypatch, capsys):
    monkeypatch.setattr(client, "missing_groups", lambda groups: [])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.cmd_provision(_args("apply"), schemas, client.config) == 1
    assert "--yes" in capsys.readouterr().out and not fake.state.data["schemas"]


def test_schema_key_clash_blocks_apply(cli, client, fake, schemas, monkeypatch, capsys):
    monkeypatch.setattr(client, "missing_groups", lambda groups: [])
    client.create_schema("Someone else's schema", "INT", "")
    assert cli.cmd_provision(_args("apply", yes=True), schemas, client.config) == 1
    assert "✗ Schema key 'INT': already used by schema 'Someone else's schema'" in capsys.readouterr().out
    assert len(fake.state.data["schemas"]) == 1


def test_too_long_description_is_dropped_with_a_warning(client, fake, schemas):
    fake.state.max_description = 20  # every schema description is longer than this
    result = provision.run(client, schemas, apply=True)
    warns = [a for a in result.actions if a.verb == "warn"]
    assert len(warns) == 3 and all("too long for Assets" in w.detail for w in warns)
    assert len(fake.state.data["schemas"]) == 3 and len(fake.state.data["objects"]) == 9


def test_disabled_cross_schema_references_stop_apply_before_objects(client, fake, schemas):
    fake.state.honour_cross_schema_flag = False  # Jira ignores the flag at creation
    result = provision.run(client, schemas, apply=True)
    blocked = sorted(a.target for a in result.actions if a.verb == "blocked")
    assert blocked == ["schema Applications", "schema Connectors"]
    assert len(fake.state.data["schemas"]) == 3 and not fake.state.data["objects"]
    for schema in fake.state.data["schemas"].values():   # the admin enables the setting in the UI
        schema["crossSchemaReferencingAllowed"] = True
    again = provision.run(client, schemas, apply=True)
    assert again.count("blocked") == 0 and len(fake.state.data["objects"]) == 9


# ---------------------------------------------------------------- API key and cache

def test_api_key_is_required_when_configured(schemas):
    directory = MockDirectory(schemas)
    http = TestClient(create_app(lambda: directory, api_key="s3cret"))
    assert http.get("/health").status_code == 200                      # health stays open
    assert http.get("/health").json()["auth"] is True
    assert http.get("/ci/EDW").status_code == 401                      # no key
    assert http.get("/ci/EDW", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert http.get("/ci/EDW", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert http.get("/ci/EDW", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert http.get("/ci/Nope", headers={"X-API-Key": "s3cret"}).status_code == 404


def test_responses_are_cached_per_ci_and_failed_side(schemas):
    directory = MockDirectory(schemas)
    http = TestClient(create_app(lambda: directory, cache_seconds=60))
    first = http.get("/ci/EDW").json()
    lookups_after_first = directory.lookups
    second = http.get("/ci/EDW").json()
    assert (first["enrichment"]["cached"], second["enrichment"]["cached"]) == (False, True)
    assert directory.lookups == lookups_after_first                    # served without new lookups
    assert {k: v for k, v in second.items() if k != "enrichment"} == {k: v for k, v in first.items() if k != "enrichment"}
    other = http.get("/ci/" + STUDENT, params={"failed_side": "target"}).json()
    assert other["enrichment"]["cached"] is False and directory.lookups > lookups_after_first


def test_cache_disabled_with_zero(schemas):
    directory = MockDirectory(schemas)
    http = TestClient(create_app(lambda: directory, cache_seconds=0))
    http.get("/ci/EDW")
    assert http.get("/ci/EDW").json()["enrichment"]["cached"] is False


def test_diagram_has_every_ci_and_relationship(schemas):
    from cmdb.diagram import mermaid
    text = mermaid(schemas)
    assert text.startswith("flowchart LR")
    for name in (EDW, "Technology One (SM)", STUDENT, EDW_CONNECTOR):
        assert name in text
    assert text.count("-->|reads from|") == 4 and text.count("-->|writes to|") == 4
    assert text.count("-.->|connects to|") == 2 and text.count(" -.-> ") == 8   # 4 integrations x 2 connectors
    assert '["Package DXMULTI-0001"]' in text and text.count("subgraph") == 1



# ---------------------------------------------------------------- integration packages

def test_package_is_derived_from_its_integrations(schemas):
    from cmdb.model import derive_packages, load_objects, package_id
    assert package_id(STUDENT) == PACKAGE and package_id("No id here") == ""
    integrations = load_objects(next(t for s in schemas for t in s.object_types if t.name == "Integration"))
    (package,) = derive_packages(integrations)
    assert package["Name"] == PACKAGE and package["Source System"] == "Technology One (SM)"
    assert package["Target System"] == EDW and package["Resolver Group"] == "Current Students"
    assert package["Connector CIs"] == f"{EDW_CONNECTOR} | CONN - Technology One Student SOAP - ADF Services"


def test_validate_flags_a_package_that_no_longer_matches(schemas, monkeypatch, tmp_path):
    import shutil
    from cmdb import model as model_module
    for f in (model_module.DATA_DIR).glob("*.csv"):
        shutil.copy(f, tmp_path / f.name)
    path = tmp_path / "integration-packages.csv"
    path.write_text(path.read_text().replace(",EDW,", ",Technology One (SM),"))
    problems = model_module.validate(schemas, tmp_path)
    assert any("Target System" in p and "its integrations give 'EDW'" in p for p in problems)


def test_package_enrichment(schemas):
    result = enrich(MockDirectory(schemas), PACKAGE)
    assert result["ci"]["type"] == "Integration Package" and result["resolver_group"] == "Current Students"
    assert result["impact"]["count"] == 4 and {r["direction"] for r in result["impact"]["integrations"]} == {"member"}
    assert result["details"]["target_systems"] == [EDW]
    assert enrich(MockDirectory(schemas), STUDENT)["details"]["package"] == PACKAGE
