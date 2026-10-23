# Handover: setting up the CMDB schemas in Jira Assets

This repo creates three Jira Assets schemas and their objects for the observability stack, and keeps
them in line with the definitions in this repo. What follows is what you need to run it in your
environment.

## What will be created

| Schema (key) | Object type | Objects |
|---|---|---|
| Applications (`APP`) | Application | Technology One (SM), EDW |
| Connectors (`CON`), optional | Connector | CONN - Data Warehouse (EDW) DB, CONN - Technology One Student SOAP - ADF Services |
| Integrations (`INT`) | Integration | ETL - DXMULTI-0001 : AppEvents from SMS to EDW (+ Student, Curriculum, Study Plan) |

Also created, per schema:
- status types `Production`, `Production - To Retire`, `Development`, `Retired`;
- reference types `Connects to`, `Reads from`, `Writes to`, `Uses`;
- the attributes in `schemas/*.yaml`.

`apply` only **creates and updates**:
- It never deletes schemas, object types, attributes or objects.
- If an existing attribute's type differs from the definition, it reports **drift** and leaves the
  attribute unchanged.

## Before you run it

1. **Service account:** create one with an API token (https://id.atlassian.com/manage-profile/security/api-tokens).
   - `apply` needs Assets admin rights, to create schemas.
   - Day-to-day lookups only need read access to the three schemas.
2. **Resolver groups: required before `apply`.** Every group in `data/*.csv` must exist in Jira:

   | Group | Used for |
   |---|---|
   | `Current Students` | Technology One (SM) and the 4 integrations |
   | `Data Squad` | EDW |
   | `Integration Squad` | the 2 connectors |

   - **Create them** in admin.atlassian.com → your site → Directory → Groups, or change the names in
     `data/*.csv` and `data/initial-load/scope-*.csv` to groups you already have.
   - **Check** with `docker compose exec cmdb cmdb preflight`, which marks each group ✓ or ✗.
   - **`apply` refuses to run** while a group is missing, so objects never end up without a resolver.
3. **Cross-schema references: required before objects can be created.** After the first `apply` has
   created the schemas, open **Applications** and **Connectors** in Assets → Schema configuration →
   General, and enable *"Allow others to select objects from this schema"*. Integrations points to
   objects in both, and Connectors points to Applications. `apply` stops before creating objects
   while this is off (marked ✗ in `preflight`). Enable it, then run `apply` again; it continues
   where it stopped.
4. **Names:** schema keys (`APP`, `CON`, `INT`), schema names, type names and attribute names are
   placeholders. If you have naming standards, edit `schemas/*.yaml` before the first `apply`.
   `preflight` fails if a key is already used by another schema.
5. **Existing application type:** if your CMDB already has one, the Applications schema is a proposal.
   Point the reference attributes in `connectors.yaml` and `integrations.yaml` (`schema:` /
   `object_type:`) at your type, and remove `schemas/applications.yaml`.

## Run

The repo runs as **one service** (`cmdb`): an always-on enrichment API, with the setup commands run
inside it.

```bash
cp .env.example .env     # JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN
docker compose up -d --build
docker compose exec cmdb cmdb validate     # offline
docker compose exec cmdb cmdb preflight    # read-only: access, schema keys, cross-schema, every resolver group
docker compose exec cmdb cmdb plan         # read-only: prerequisites, then the list of changes
docker compose exec cmdb cmdb apply        # re-checks, shows the plan, asks "Apply N change(s)? [y/N]"
docker compose exec cmdb cmdb plan         # expect: 0 create, 0 update
curl -H "Authorization: Bearer <CMDB_API_KEY>" http://localhost:8090/ci/EDW   # read back: Data Squad, impact 4
```

**Not using Compose?** Open a shell in the container (`docker exec -it observability-cmdb sh`) and run
`cmdb preflight`, `cmdb plan`, `cmdb apply`. See "Running without Docker Compose" in the README.

**Permissions:**
- `apply` needs **Assets admin** rights. Pass an admin token just for that command:
  `docker compose exec -e JIRA_EMAIL=… -e JIRA_API_TOKEN=… cmdb cmdb apply`.
- The running service only needs **read** access.
- The API itself is protected by `CMDB_API_KEY` (set it in `.env` or as a secret). Callers send it as
  `Authorization: Bearer <key>`.

`plan` output marks each item:

| Mark | Meaning |
|---|---|
| `+` | create |
| `~` | update (lists the changed attributes) |
| `!` | drift (left unchanged) |
| `?` | warning |

The prerequisites block at the top marks each check ✓ (passed), ✗ (failed: `apply` won't run) or ?
(couldn't be verified: `apply` won't run unless `--skip-group-check` is given).

## After setup

- **Objects** are maintained in Jira Assets, which is now the source of truth.
- **Structure** changes go through `schemas/*.yaml` and `plan`/`apply`, so the definitions and Jira
  stay in step.
- **The enrichment API** (the running `cmdb` service) only needs read access. Keep calls
  `http://cmdb:8080/ci/{ci_name}` with each alert's CI name to get the resolver group and impact.

## If something fails

Errors show the exact API call and HTTP status, for example
`HTTP 404 for GET /config/referencetype …`. The API calls are tested against an in-repo fake of the
Assets API, so your instance is the first real one. Every endpoint is listed in `cmdb/assets.py`.
Send the error back if one doesn't match your Jira's API.
