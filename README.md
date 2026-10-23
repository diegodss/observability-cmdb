# Observability CMDB

The Jira Assets side of the observability stack:

1. **Schemas as code.** Three Jira Assets schemas (Applications, Connectors, Integrations) are
   defined in `schemas/*.yaml`, with their objects in `data/*.csv` (loaded once from the
   Integrations SOT).
2. **`plan` / `apply`.** These make Jira Assets match those files through the Assets REST API.
   `plan` is read-only. `apply` creates and updates, and never deletes.
3. **Enrichment.** Give it a CI name and it returns the resolver group, the root CI, who to inform
   and the impact. It's available as a command (`lookup`) and as an HTTP service (`enricher`) that
   Keep in the [observability-platform](https://github.com/diegodss/observability-platform) calls with
   each alert's `ci_name`.

```
schemas/*.yaml + data/*.csv ──plan/apply──▶ Jira Assets ◀──lookup── enricher ◀── Keep (ci_name from the alert)
```

Part of a three-repo setup:
- [observability-env-simulator](https://github.com/diegodss/observability-env-simulator): the systems being monitored;
- [observability-platform](https://github.com/diegodss/observability-platform): Prometheus, Grafana, alerting, and Keep later;
- this repo.

Everything runs in Docker; nothing needs installing on the host except Docker.

## Quick start

```bash
cp .env.example .env        # then set JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN
docker compose build
```

| Step | Command | What it does |
|---|---|---|
| 1 | `docker compose run --rm cmdb validate` | Offline check of the schema files and data |
| 2 | `docker compose run --rm cmdb plan` | **Read-only.** Connects to Jira Assets and lists what would be created or updated |
| 3 | `docker compose run --rm cmdb apply` | Creates the 3 schemas, their statuses, reference types, object types, attributes and the 8 objects |
| 4 | `docker compose run --rm cmdb plan` | Should now report `0 create, 0 update` |
| 5 | `docker compose run --rm cmdb lookup EDW` | Enriches an application CI from Jira |
| 6 | `docker compose run --rm cmdb lookup "ETL - DXMULTI-0001 : AppEvents from SMS to EDW - Student"` | Enriches an integration CI |
| 7 | `docker compose up -d enricher` | Starts the HTTP service on http://localhost:8090 |

Notes:
- **Verbose output:** add `-v` to `plan`/`apply` to also list what's already in place.
- **Integration root CI:** add `--failed-side target` (or `source`) to `lookup` to treat that
  application as the root CI, as Keep does when the alert says the target failed.
- **Without Jira:** set `CMDB_BACKEND=mock` to run `lookup` and the `enricher` from the local data files.

Expected `lookup EDW`:

```
  CI              EDW (Application, Applications, APP-2)
  Status          Production
  Root CI         itself
  Resolver group  Data Squad
  Informed        Current Students
  Impact          4 integration(s); Current Students / Student Enrolment
  Aliases         EDW (SVC_IDM) | EDW(DI_SMS_EDW) | EDW (di_edw_cms) | EDW / StudentOne

  Affected integrations:
    - ETL - DXMULTI-0001 : AppEvents from SMS to EDW  [writes to, resolver: Current Students]
    - ETL - DXMULTI-0001 : AppEvents from SMS to EDW - Curriculum  [writes to, resolver: Current Students]
    - ETL - DXMULTI-0001 : AppEvents from SMS to EDW - Student  [writes to, resolver: Current Students]
    - ETL - DXMULTI-0001 : AppEvents from SMS to EDW - Study Plan  [writes to, resolver: Current Students]
```

### Jira prerequisites

- **Assets enabled:** Jira Service Management with Assets.
- **API token** for the account in `.env`, from https://id.atlassian.com/manage-profile/security/api-tokens.
  - `apply` needs Assets admin rights, to create schemas.
  - `lookup` and the `enricher` only need read access to the three schemas.
- **Resolver groups exist** as Jira groups: `Current Students`, `Data Squad`, `Integration & Platforms`.
  `plan` warns about any that are missing. The names are placeholders; see [docs/jira-team-handover.md](docs/jira-team-handover.md).

**First run against your Jira:** the Assets API calls are tested against an in-repo fake of the API
(`tests/fake_assets.py`), not yet against a real Jira. Run `plan` first: it only reads. If any
endpoint behaves differently, the error names the exact call and HTTP status. Every endpoint is in
[`cmdb/assets.py`](cmdb/assets.py).

## The schemas

| Schema (key) | Object type | Attributes | Objects |
|---|---|---|---|
| **Applications** (`APP`) | Application | Name, Description, Aliases, Resolver Group, Status, Hosting | Technology One (SM), EDW |
| **Connectors** (`CON`), optional | Connector | Name, Description, Connects To → Application, Platform, Resolver Group, Status | 2 Boomi connectors |
| **Integrations** (`INT`) | Integration | Name, Description, Source System / Target System → Application, Connector CIs → Connector (many), Resolver Group, Status, plus 21 attributes named after the SOT columns | the 4 ETL - DXMULTI-0001 AppEvents flows |

Assets built-in types are used for **Resolver Group** (Group, a real Jira group) and **Status**
(status types `Production`, `Production - To Retire`, `Development`, `Retired`). The references
between schemas use the reference types *Connects to*, *Reads from*, *Writes to* and *Uses*.

To change the structure, edit `schemas/*.yaml`, then run `plan` and `apply`. Schema, type and
attribute names are placeholders that can be renamed to the organisation's standard.

## Enrichment API (for Keep)

`docker compose up -d enricher` serves:

| Endpoint | Response |
|---|---|
| `GET /health` | `{"status": "ok", "backend": "jira"}` |
| `GET /ci/{ci_name}` | enrichment JSON; **404** with `"found": false` for an unknown CI; **502** if Jira is unreachable |
| `GET /ci/{ci_name}?failed_side=target` | as above, with the integration's target (or `source`) application as root CI |

URL-encode the name, e.g. `/ci/ETL%20-%20DXMULTI-0001%20%3A%20AppEvents%20from%20SMS%20to%20EDW%20-%20Student`.

```json
{
  "found": true,
  "ci":       {"name": "...Student", "type": "Integration", "schema": "Integrations", "key": "INT-1", "status": "Production", "resolver_group": "Current Students"},
  "root_ci":  {"name": "EDW", "type": "Application", "schema": "Applications", "key": "APP-2", "status": "Production", "resolver_group": "Data Squad"},
  "resolver_group": "Data Squad",
  "informed_groups": ["Current Students"],
  "impact": {"count": 4, "domains": ["Current Students / Student Enrolment"],
             "integrations": [{"name": "...", "direction": "writes to", "resolver_group": "Current Students", "status": "Production",
                               "domain": "Current Students", "subdomain": "Student Enrolment",
                               "source_system": "Technology One (SM)", "target_system": "EDW"}]},
  "details": {"domain": "...", "subdomain": "...", "source_system": {...}, "target_system": {...},
              "platform": "Boomi", "schedule": "...", "connectors": "...", "connector_cis": [...], "log_source": "..."},
  "enrichment": {"backend": "jira", "lookups": 6, "at": "2026-10-24T06:50:00+00:00"}
}
```

Root CI rules:
- **Application:** itself. The impact is every integration that reads from or writes to it.
- **Connector:** itself. The impact is every integration whose Connector CIs include it.
- **Integration:** itself, unless `failed_side` is `source` or `target`; then that application, with
  that application's impact.

### Plugging into the platform

The enricher joins the shared Docker network `observability-sim`, so containers in the other two repos
reach it at `http://enricher:8080`. Start the stacks in any order. The second one to start logs a
harmless "network was not created for project" warning.

A Keep workflow step calls it with the alert's label. Keep's provider syntax changes between versions,
so check it against the Keep docs:

```yaml
steps:
  - name: enrich-from-cmdb
    provider:
      type: http
      with:
        method: GET
        url: "http://enricher:8080/ci/{{ alert.labels.ci_name }}?failed_side={{ alert.labels.failed_side }}"
```

The ticket templates in the platform repo (`docs/incident-templates.md`) take their **Resolver**,
**Impact**, **Domain** and **Flow** values from this response.

## Initial load from the SOT (once)

`data/*.csv` are generated from the Integrations SOT and the scope files in `data/initial-load/`:
- which integrations to load;
- application aliases;
- resolver groups;
- which connectors are CIs.

```bash
docker compose run --rm -v "/folder/with/the/sot:/input:ro" cmdb initial-load "/input/Integrations SOT(Integrations).csv"
docker compose run --rm cmdb validate
```

After the first `apply`, **Jira Assets is the source of truth**. Re-running `initial-load` and `apply`
would overwrite edits made in Jira for the same objects. Only do that to add new integrations: put just
those in the scope files. The SOT file is internal and isn't in this repo.

## Tests

```bash
docker compose run --rm --entrypoint pytest cmdb
```

12 tests, using the fake Assets API:
- definitions are consistent;
- `plan` changes nothing;
- `apply` builds everything and is idempotent;
- a changed value updates only that attribute;
- the Jira and mock backends give identical enrichment for EDW, an integration (normal and target-side
  failure), a connector and an unknown CI;
- the HTTP service, including 404 and input validation.

## Layout

```
schemas/                 the 3 schema definitions (structure as code)
data/                    objects per type (CSV), generated by initial-load
data/initial-load/       scope files for the one-off SOT load
cmdb/model.py            loads and validates schemas + data
cmdb/assets.py           Jira Assets REST client (every endpoint used)
cmdb/provision.py        plan / apply
cmdb/directory.py        CI reads from Jira or from the local files (mock)
cmdb/enrich.py           resolver group, root CI, impact
cmdb/server.py           HTTP enrichment service
tests/                   fake Assets API + tests
docs/jira-team-handover.md
```
