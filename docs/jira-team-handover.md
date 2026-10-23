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
2. **Resolver groups:** the definitions use these Jira groups. Create them, or map the placeholders to
   your real groups:

   | Placeholder in `data/*.csv` | Used for |
   |---|---|
   | `Current Students` | Technology One (SM) and the 4 integrations |
   | `Data Squad` | EDW |
   | `Integration & Platforms` | the 2 connectors |

   Group values that don't match an existing group aren't stored, and `plan` lists any that are missing.
3. **Names:** schema keys (`APP`, `CON`, `INT`), schema names, type names and attribute names are
   placeholders. If you have naming standards, edit `schemas/*.yaml` before the first `apply`.
4. **Existing application type:** if your CMDB already has one, the Applications schema is a proposal.
   Point the reference attributes in `connectors.yaml` and `integrations.yaml` (`schema:` /
   `object_type:`) at your type, and remove `schemas/applications.yaml`.

## Run

```bash
cp .env.example .env     # JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN
docker compose build
docker compose run --rm cmdb validate     # offline
docker compose run --rm cmdb plan         # read-only: review the list
docker compose run --rm cmdb apply
docker compose run --rm cmdb plan         # expect: 0 create, 0 update
docker compose run --rm cmdb lookup EDW   # read back: resolver group Data Squad, impact 4 integrations
```

`plan` output marks each item:

| Mark | Meaning |
|---|---|
| `+` | create |
| `~` | update (lists the changed attributes) |
| `!` | drift (left unchanged) |
| `?` | warning (e.g. missing Jira groups) |

## After setup

- **Objects** are maintained in Jira Assets, which is now the source of truth.
- **Structure** changes go through `schemas/*.yaml` and `plan`/`apply`, so the definitions and Jira
  stay in step.
- **The enrichment service** (`docker compose up -d enricher`) only needs read access. Keep calls it
  with each alert's CI name to get the resolver group and impact.

## If something fails

Errors show the exact API call and HTTP status, for example
`HTTP 404 for GET /config/referencetype …`. The API calls are tested against an in-repo fake of the
Assets API, so your instance is the first real one. Every endpoint is listed in `cmdb/assets.py`.
Send the error back if one doesn't match your Jira's API.
