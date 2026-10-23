"""One-off initial load: turn Integrations SOT rows into the data/*.csv object files.

The SOT is only the starting point. After `apply` has loaded the objects into Jira
Assets, Jira Assets is the source of truth and this is not run again for them.

Inputs (data/initial-load/):
  scope-integrations.csv  which SOT rows to load, plus Resolver Group and Description
  scope-applications.csv  canonical application names, Aliases (SOT spellings),
                          Resolver Group, Status, Description
  scope-connectors.csv    OPTIONAL: connectors that exist as CIs, with Connects To,
                          Resolver Group, Status, Description
"""

import csv
import re
from pathlib import Path

from .model import DATA_DIR, Schema, derive_packages, package_id

SCOPE_DIR = DATA_DIR / "initial-load"
NUMERIC = {"Volume per execution (MB)", "Volume per week (MB)"}


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, header: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _clean(column: str, value: str) -> str:
    value = (value or "").strip()
    if column in NUMERIC and not re.fullmatch(r"\d+(\.\d+)?", value):
        return ""  # 'No Data', 'Not recorded' -> empty
    if column == "Connectors":
        return " | ".join(part.strip() for part in value.split("|") if part.strip())
    return value


def _columns(schemas: list[Schema], object_type: str) -> list[str]:
    for schema in schemas:
        for candidate in schema.object_types:
            if candidate.name == object_type:
                return [a.name for a in candidate.attributes]
    raise KeyError(object_type)


def run(sot_path: Path, schemas: list[Schema], scope_dir: Path = SCOPE_DIR, out_dir: Path = DATA_DIR) -> tuple[dict, list[str]]:
    sot = {row["Integration Name"].strip(): row for row in _read(sot_path)}
    applications = _read(scope_dir / "scope-applications.csv")
    integrations = _read(scope_dir / "scope-integrations.csv")
    connector_scope = scope_dir / "scope-connectors.csv"
    connectors = _read(connector_scope) if connector_scope.exists() else []
    connector_names = {c["Name"] for c in connectors}
    connector_platforms: dict[str, set[str]] = {name: set() for name in connector_names}
    problems: list[str] = []

    canonical = {}
    for app in applications:
        canonical[app["Name"]] = app["Name"]
        for alias in filter(None, (a.strip() for a in app["Aliases"].split("|"))):
            canonical[alias] = app["Name"]
    hosting: dict[str, set[str]] = {app["Name"]: set() for app in applications}

    integration_columns = _columns(schemas, "Integration")
    integration_rows = []
    for scoped in integrations:
        name = scoped["Integration Name"]
        row = sot.get(name)
        if row is None:
            problems.append(f"'{name}' is not in the SOT")
            continue
        out = {column: _clean(column, row[column]) for column in integration_columns if column in row}
        out.update(Name=name, Description=scoped["Description"], **{"Resolver Group": scoped["Resolver Group"]})
        for column, hosting_column in (("Source System", "Source Hosting"), ("Target System", "Target Hosting")):
            value = row[column].strip()
            out[column] = canonical.get(value, "")
            if not out[column]:
                problems.append(f"'{name}': {column} '{value}' is not a scoped application or alias")
            elif row[hosting_column].strip():
                hosting[out[column]].add(row[hosting_column].strip())
        used = [c for c in out.get("Connectors", "").split(" | ") if c in connector_names]
        for connector in used:
            connector_platforms[connector].add(row["Platform"].strip())
        out["Connector CIs"] = " | ".join(used)
        out["Package"] = package_id(name)
        integration_rows.append(out)

    app_names = {app["Name"] for app in applications}
    connector_rows = []
    for connector in connectors:
        name = connector["Name"]
        if not connector_platforms[name]:
            problems.append(f"connector '{name}' is not used by any scoped integration in the SOT")
        if connector["Connects To"] and connector["Connects To"] not in app_names:
            problems.append(f"connector '{name}': Connects To '{connector['Connects To']}' is not a scoped application")
        platforms = connector_platforms[name]
        connector_rows.append({**connector, "Platform": next(iter(platforms)) if len(platforms) == 1 else ""})

    application_rows = [{**app, "Hosting": " | ".join(sorted(hosting[app["Name"]]))} for app in applications]

    package_rows = derive_packages(integration_rows)
    counts = {"applications": len(application_rows), "connectors": len(connector_rows),
              "packages": len(package_rows), "integrations": len(integration_rows), "sot_rows": len(sot)}
    if problems:
        return counts, problems
    _write(out_dir / "applications.csv", _columns(schemas, "Application"), application_rows)
    connector_file = out_dir / "connectors.csv"
    if connector_rows:
        _write(connector_file, _columns(schemas, "Connector"), connector_rows)
    elif connector_file.exists():
        connector_file.unlink()  # connectors are optional: no scope file, no Connector objects
    _write(out_dir / "integration-packages.csv", _columns(schemas, "Integration Package"), package_rows)
    _write(out_dir / "integrations.csv", integration_columns, integration_rows)
    return counts, problems
