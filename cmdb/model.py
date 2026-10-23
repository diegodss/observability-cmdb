"""Schema definitions (schemas/*.yaml) and object data (data/*.csv)."""

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
DATA_DIR = ROOT / "data"
MULTI_SEPARATOR = "|"

# Attribute type -> (Assets attribute "type", Assets "defaultTypeId") for default types.
DEFAULT_TYPES = {"text": 0, "integer": 1, "boolean": 2, "float": 3, "date": 4, "url": 7, "email": 8,
                 "textarea": 9, "select": 10}
ATTRIBUTE_TYPES = set(DEFAULT_TYPES) | {"label", "reference", "group", "status"}


@dataclass
class Attribute:
    name: str
    type: str
    required: bool = False
    unique: bool = False
    multiple: bool = False
    options: list[str] = field(default_factory=list)
    schema: str = ""            # reference target schema
    object_type: str = ""       # reference target object type
    reference_type: str = ""    # reference type name
    description: str = ""

    def values(self, raw: str) -> list[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        if self.multiple:
            return [v.strip() for v in raw.split(MULTI_SEPARATOR) if v.strip()]
        return [raw]


@dataclass
class ObjectType:
    name: str
    description: str
    icon: str
    data: str
    attributes: list[Attribute]

    def attribute(self, name: str) -> Attribute:
        return next(a for a in self.attributes if a.name == name)


@dataclass
class Schema:
    name: str
    key: str
    description: str
    statuses: list[str]
    reference_types: list[dict]
    object_types: list[ObjectType]

    def depends_on(self) -> set[str]:
        return {a.schema for t in self.object_types for a in t.attributes
                if a.type == "reference" and a.schema != self.name}


def load_schemas(directory: Path = SCHEMA_DIR) -> list[Schema]:
    """Load every schema file, ordered so referenced schemas come first."""
    schemas = []
    for path in sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        types = [ObjectType(name=t["name"], description=t.get("description", ""), icon=t.get("icon", ""),
                            data=t.get("data", ""),
                            attributes=[Attribute(**a) for a in t["attributes"]])
                 for t in doc["object_types"]]
        schemas.append(Schema(name=doc["schema"]["name"], key=doc["schema"]["key"],
                              description=doc["schema"].get("description", ""),
                              statuses=doc.get("statuses", []), reference_types=doc.get("reference_types", []),
                              object_types=types))
    ordered, remaining = [], {s.name: s for s in schemas}
    while remaining:
        ready = [s for s in remaining.values() if s.depends_on() <= {o.name for o in ordered}]
        if not ready:
            raise ValueError(f"circular or missing schema references among {sorted(remaining)}")
        for schema in sorted(ready, key=lambda s: s.name):
            ordered.append(remaining.pop(schema.name))
    return ordered


def load_objects(object_type: ObjectType, directory: Path = DATA_DIR) -> list[dict]:
    path = directory / object_type.data
    if not object_type.data or not path.exists():
        return []
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


PACKAGE_ID = re.compile(r"\b([A-Z]{4,}-\d{4}[a-z]?)\b")


def package_id(integration_name: str) -> str:
    """The integration ID in an integration's name, e.g. 'DXMULTI-0001'; '' if none."""
    match = PACKAGE_ID.search(integration_name)
    return match.group(1) if match else ""


def derive_packages(integrations: list[dict]) -> list[dict]:
    """Integration Package rows derived from the integrations that carry each ID."""
    def multi(rows, column):
        return " | ".join(sorted({v.strip() for r in rows for v in r.get(column, "").split(MULTI_SEPARATOR) if v.strip()}))

    groups: dict[str, list[dict]] = {}
    for row in integrations:
        if package_id(row["Name"]):
            groups.setdefault(package_id(row["Name"]), []).append(row)
    packages = []
    for name, rows in sorted(groups.items()):
        resolvers = {r.get("Resolver Group", "") for r in rows}
        sources, targets = multi(rows, "Source System"), multi(rows, "Target System")
        packages.append({
            "Name": name,
            "Description": f"{len(rows)} integration(s) with ID {name}: {sources or '?'} to {targets or '?'}",
            "Source System": sources, "Target System": targets, "Connector CIs": multi(rows, "Connector CIs"),
            "Resolver Group": resolvers.pop() if len(resolvers) == 1 else "",
        })
    return packages


def _package_problems(schemas: list[Schema], directory: Path) -> list[str]:
    types = {t.name: t for s in schemas for t in s.object_types}
    if "Integration Package" not in types or "Integration" not in types:
        return []
    integrations = load_objects(types["Integration"], directory)
    stored = {r["Name"]: r for r in load_objects(types["Integration Package"], directory)}
    problems = [f"Integration '{r['Name']}': Package should be '{package_id(r['Name'])}', not '{r.get('Package', '')}'"
                for r in integrations if r.get("Package", "") != package_id(r["Name"])]
    columns = [a.name for a in types["Integration Package"].attributes]
    for derived in derive_packages(integrations):
        current = stored.pop(derived["Name"], None)
        if current is None:
            problems.append(f"Integration Package '{derived['Name']}' is missing; run initial-load to regenerate it")
            continue
        for column in columns:
            if column in derived and current.get(column, "") != derived[column]:
                problems.append(f"Integration Package '{derived['Name']}': {column} is '{current.get(column, '')}' "
                                f"but its integrations give '{derived[column]}'")
    problems += [f"Integration Package '{name}' has no integrations" for name in stored]
    return problems


ROUTING_FILE = "routing.yaml"
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")


def load_routing(directory: Path = DATA_DIR) -> dict:
    path = directory / ROUTING_FILE
    doc = yaml.safe_load(path.read_text()) if path.exists() else {}
    return {"default_project": str(doc.get("default_project") or ""), "label_prefix": str(doc.get("label_prefix", "resolver-")),
            "groups": {str(k): (v or {}) for k, v in (doc.get("groups") or {}).items()}}


def route(resolver_group: str, routing: dict) -> dict:
    """Ticket destination for a resolver group: its own project if set, else the default project + a label."""
    slug = re.sub(r"[^a-z0-9]+", "-", (resolver_group or "unassigned").lower()).strip("-")
    project = str((routing["groups"].get(resolver_group) or {}).get("project") or "")
    return {"resolver_group": resolver_group, "mode": "project" if project else "label",
            "project": project or routing["default_project"], "labels": [routing["label_prefix"] + slug]}


def _routing_problems(schemas: list[Schema], directory: Path) -> list[str]:
    if not (directory / ROUTING_FILE).exists():
        return [f"data/{ROUTING_FILE} is missing"]
    routing = load_routing(directory)
    problems = []
    if not PROJECT_KEY.match(routing["default_project"]):
        problems.append(f"{ROUTING_FILE}: default_project '{routing['default_project']}' is not a Jira project key")
    for group, entry in routing["groups"].items():
        project = str(entry.get("project") or "")
        if project and not PROJECT_KEY.match(project):
            problems.append(f"{ROUTING_FILE}: project '{project}' for '{group}' is not a Jira project key")
    used = {v for s in schemas for t in s.object_types for a in t.attributes if a.type == "group"
            for r in load_objects(t, directory) for v in a.values(r.get(a.name, ""))}
    problems += [f"{ROUTING_FILE}: resolver group '{g}' is used in data/*.csv but has no entry under groups"
                 for g in sorted(used - set(routing["groups"]))]
    return problems


def validate(schemas: list[Schema], directory: Path = DATA_DIR) -> list[str]:
    """Offline checks of the schema files and data, the way an import would see them."""
    problems = []
    names = {(s.name, t.name): {r["Name"] for r in load_objects(t, directory)}
             for s in schemas for t in s.object_types}
    for schema in schemas:
        ref_types = {r["name"] for r in schema.reference_types}
        for object_type in schema.object_types:
            label = f"{schema.name}/{object_type.name}"
            rows = load_objects(object_type, directory)
            columns = set(rows[0]) if rows else set()
            if not any(a.type == "label" for a in object_type.attributes):
                problems.append(f"{label}: needs one attribute of type 'label'")
            for column in columns - {a.name for a in object_type.attributes}:
                problems.append(f"{label}: data column '{column}' is not an attribute")
            for attribute in object_type.attributes:
                if attribute.type not in ATTRIBUTE_TYPES:
                    problems.append(f"{label}.{attribute.name}: unknown type '{attribute.type}'")
                    continue
                if attribute.type == "reference":
                    if (attribute.schema, attribute.object_type) not in names:
                        problems.append(f"{label}.{attribute.name}: references unknown {attribute.schema}/{attribute.object_type}")
                    if attribute.reference_type not in ref_types:
                        problems.append(f"{label}.{attribute.name}: reference type '{attribute.reference_type}' not declared in {schema.name}")
                values = [r.get(attribute.name, "") for r in rows]
                if (attribute.required or attribute.type == "label") and rows and not all(v.strip() for v in values):
                    problems.append(f"{label}: '{attribute.name}' is required but empty in some rows")
                if attribute.type == "label":
                    dupes = sorted({v for v in values if values.count(v) > 1})
                    if dupes:
                        problems.append(f"{label}: duplicate names {dupes}")
                for row in rows:
                    for value in attribute.values(row.get(attribute.name, "")):
                        error = _value_error(attribute, value, schema, names)
                        if error:
                            problems.append(f"{label} '{row.get('Name')}': {attribute.name}: {error}")
    return problems + _package_problems(schemas, directory) + _routing_problems(schemas, directory)


def _value_error(attribute: Attribute, value: str, schema: Schema, names: dict) -> str | None:
    if attribute.type == "select" and value not in attribute.options:
        return f"'{value}' not in {attribute.options}"
    if attribute.type == "status" and value not in schema.statuses:
        return f"'{value}' not in statuses {schema.statuses}"
    if attribute.type == "reference" and value not in names.get((attribute.schema, attribute.object_type), set()):
        return f"'{value}' is not a {attribute.schema}/{attribute.object_type}"
    try:
        if attribute.type == "integer":
            int(value)
        elif attribute.type == "float":
            float(value)
        elif attribute.type == "date":
            date.fromisoformat(value)
        elif attribute.type == "url" and not re.match(r"https?://", value):
            raise ValueError
    except ValueError:
        return f"'{value}' is not a valid {attribute.type}"
    return None
