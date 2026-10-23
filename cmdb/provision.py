"""Plan / apply: make Jira Assets match schemas/*.yaml and data/*.csv.

Idempotent and additive: it creates what is missing and updates object attribute
values that differ. It never deletes schemas, types, attributes or objects, and it
reports (but does not change) attributes whose type differs from the definition.
"""

from dataclasses import dataclass, field

from .assets import AssetsClient, AssetsError
from .model import DEFAULT_TYPES, Attribute, ObjectType, Schema, load_objects

CROSS_SCHEMA_HELP = ("cross-schema references are disabled: in Assets open the schema > Schema configuration > "
                     "General and enable 'Allow others to select objects from this schema', then re-run apply")

# Status category: 0 = inactive, 1 = active, 2 = pending
STATUS_CATEGORY = {"Production": 1, "Production - To Retire": 1, "Development": 2, "Retired": 0}
ASSETS_TYPE = {"reference": 1, "group": 4, "status": 7}


@dataclass
class Action:
    verb: str        # create | update | ok | drift | warn
    target: str
    detail: str = ""


@dataclass
class Result:
    actions: list[Action] = field(default_factory=list)

    def add(self, verb: str, target: str, detail: str = "") -> None:
        self.actions.append(Action(verb, target, detail))

    def count(self, verb: str) -> int:
        return sum(a.verb == verb for a in self.actions)


def _by_name(items: list[dict]) -> dict[str, dict]:
    return {str(i.get("name")): i for i in items}


class Provisioner:
    def __init__(self, client: AssetsClient, schemas: list[Schema], apply: bool):
        self.client, self.schemas, self.apply = client, schemas, apply
        self.result = Result()
        self.schema_ids: dict[str, str | None] = {}
        self.type_ids: dict[tuple[str, str], str | None] = {}
        self.status_ids: dict[tuple[str, str], str | None] = {}
        self.reftype_ids: dict[tuple[str, str], str | None] = {}
        self.object_keys: dict[tuple[str, str, str], str | None] = {}   # (schema, type, name) -> object key
        self._icons: list[dict] | None = None

    def run(self) -> Result:
        for schema in self.schemas:
            self._schema(schema)
        for schema in self.schemas:
            for object_type in schema.object_types:
                self._attributes(schema, object_type)
        if self._cross_schema_blocked() and self.apply:
            return self.result  # objects would be rejected by Jira; stop before creating any
        for schema in self.schemas:
            for object_type in schema.object_types:
                self._objects(schema, object_type)
        return self.result

    def _cross_schema_blocked(self) -> bool:
        """Schemas referenced from other schemas need 'Allow others to select objects' enabled."""
        blocked = False
        settings = {s["name"]: s for s in self.client.schemas()}
        for name in sorted(referenced_schemas(self.schemas)):
            current = settings.get(name)
            if current is None or current.get("crossSchemaReferencingAllowed", True):
                continue
            blocked = True
            self.result.add("blocked", f"schema {name}", CROSS_SCHEMA_HELP)
        return blocked

    # ------------------------------------------------------------------ structure
    def _schema(self, schema: Schema) -> None:
        existing = _by_name(self.client.schemas()).get(schema.name)
        if existing:
            schema_id = str(existing["id"])
            self.result.add("ok", f"schema {schema.name}", f"key {existing.get('objectSchemaKey', '?')}")
        elif self.apply:
            created = self._with_description(f"schema {schema.name}", schema.description,
                                             lambda d: self.client.create_schema(
                                                 schema.name, schema.key, d,
                                                 allow_cross_schema=schema.name in referenced_schemas(self.schemas)))
            schema_id = str(created["id"])
            self.result.add("create", f"schema {schema.name}", f"key {schema.key}")
        else:
            schema_id = None
            self.result.add("create", f"schema {schema.name}", f"key {schema.key}")
        self.schema_ids[schema.name] = schema_id

        current = _by_name(self.client.status_types(schema_id)) if schema_id else {}
        for status in schema.statuses:
            self.status_ids[(schema.name, status)] = self._ensure(
                current.get(status), f"status {schema.name}/{status}",
                lambda s=status: self.client.create_status_type(schema_id, s, STATUS_CATEGORY.get(s, 1)))

        current = _by_name(self.client.reference_types(schema_id)) if schema_id else {}
        for ref in schema.reference_types:
            self.reftype_ids[(schema.name, ref["name"])] = self._ensure(
                current.get(ref["name"]), f"reference type {schema.name}/{ref['name']}",
                lambda r=ref: self.client.create_reference_type(schema_id, r["name"], r.get("color", "999999")))

        current = _by_name(self.client.object_types(schema_id)) if schema_id else {}
        for object_type in schema.object_types:
            self.type_ids[(schema.name, object_type.name)] = self._ensure(
                current.get(object_type.name), f"object type {schema.name}/{object_type.name}",
                lambda t=object_type: self._with_description(
                    f"object type {schema.name}/{t.name}", t.description,
                    lambda d: self.client.create_object_type(schema_id, t.name, d, self._icon(t.icon))))

    def _ensure(self, existing: dict | None, target: str, create) -> str | None:
        if existing:
            self.result.add("ok", target)
            return str(existing["id"])
        self.result.add("create", target)
        return str(create()["id"]) if self.apply else None

    def _with_description(self, target: str, description: str, create):
        """Create with the description; if Assets rejects it as too long, create without it and warn."""
        try:
            return create(description)
        except AssetsError as exc:
            if not description or "Size.description" not in str(exc):
                raise
            self.result.add("warn", target, f"description ({len(description)} chars) too long for Assets; created without it")
            return create("")

    def _icon(self, name: str) -> str:
        if self._icons is None:
            self._icons = self.client.icons()
        match = next((i for i in self._icons if str(i.get("name", "")).lower() == name.lower()), None)
        return str((match or self._icons[0])["id"])

    def _attributes(self, schema: Schema, object_type: ObjectType) -> None:
        type_id = self.type_ids[(schema.name, object_type.name)]
        current = _by_name(self.client.attributes(type_id)) if type_id else {}
        for attribute in object_type.attributes:
            target = f"attribute {schema.name}/{object_type.name}.{attribute.name}"
            existing = current.get(attribute.name)
            if attribute.type == "label":
                if existing and not existing.get("label"):
                    self.result.add("drift", target, "exists but is not the label attribute")
                elif existing:
                    self.result.add("ok", target, "label")
                elif type_id:
                    self.result.add("warn", target, "object type has no 'Name' attribute; Assets normally creates it as the label")
                continue
            if existing:
                expected = self._body(schema, attribute)
                if int(existing.get("type", 0)) != expected["type"] or (
                        expected["type"] == 0 and int(existing.get("defaultType", {}).get("id", expected["defaultTypeId"])) != expected["defaultTypeId"]):
                    self.result.add("drift", target, f"type differs from definition ({attribute.type}); left unchanged")
                else:
                    self.result.add("ok", target)
                continue
            self.result.add("create", target, attribute.type)
            if self.apply and type_id:
                body = self._body(schema, attribute)
                self._with_description(target, body["description"],
                                       lambda d: self.client.create_attribute(type_id, {**body, "description": d}))

    def _body(self, schema: Schema, attribute: Attribute) -> dict:
        body = {"name": attribute.name, "description": attribute.description,
                "minimumCardinality": 1 if attribute.required else 0,
                "maximumCardinality": -1 if attribute.multiple else 1,
                "uniqueAttribute": attribute.unique}
        if attribute.type in DEFAULT_TYPES:
            body.update(type=0, defaultTypeId=DEFAULT_TYPES[attribute.type])
            if attribute.options:
                body["options"] = ",".join(attribute.options)
        elif attribute.type == "reference":
            body.update(type=1, typeValue=self.type_ids.get((attribute.schema, attribute.object_type)),
                        additionalValue=self.reftype_ids.get((schema.name, attribute.reference_type)))
        elif attribute.type == "group":
            body.update(type=4)
        elif attribute.type == "status":
            body.update(type=7, typeValueMulti=[self.status_ids.get((schema.name, s)) for s in schema.statuses])
        return body

    # ------------------------------------------------------------------ objects
    def _objects(self, schema: Schema, object_type: ObjectType) -> None:
        rows = load_objects(object_type)
        type_id = self.type_ids[(schema.name, object_type.name)]
        existing = {}
        if type_id:
            query = f'objectSchema = "{schema.name}" AND objectType = "{object_type.name}"'
            existing = {o["label"]: o for o in self.client.aql(query)}
        attr_ids = {a["name"]: str(a["id"]) for a in self.client.attributes(type_id, refresh=True)} if type_id else {}
        for row in rows:
            name = row["Name"]
            target = f"object {schema.name}/{object_type.name} '{name}'"
            current = existing.get(name)
            self.object_keys[(schema.name, object_type.name, name)] = current["key"] if current else None
            changes = self._diff(schema, object_type, row, current)
            if current and not changes:
                self.result.add("ok", target)
                continue
            verb = "update" if current else "create"
            self.result.add(verb, target, ", ".join(changes) if current else "")
            if self.apply:
                payload = self._payload(schema, object_type, row, attr_ids, only=None if not current else changes)
                if current:
                    self.client.update_object(current["id"], type_id, payload)
                else:
                    created = self.client.create_object(type_id, payload)
                    self.object_keys[(schema.name, object_type.name, name)] = created.get("objectKey")

    def _diff(self, schema: Schema, object_type: ObjectType, row: dict, current: dict | None) -> list[str]:
        changed = []
        for attribute in object_type.attributes:
            if attribute.type == "label":
                continue
            want = sorted(_normal(attribute, v) for v in attribute.values(row.get(attribute.name, "")))
            have = sorted(_normal(attribute, v) for v in (current or {}).get("attrs", {}).get(attribute.name, []))
            if want != have:
                changed.append(attribute.name)
        return changed

    def _payload(self, schema: Schema, object_type: ObjectType, row: dict, attr_ids: dict, only: list[str] | None) -> list[dict]:
        payload = []
        for attribute in object_type.attributes:
            if only is not None and attribute.name not in only and attribute.type != "label":
                continue
            values = []
            for value in attribute.values(row.get(attribute.name, "")):
                if attribute.type == "reference":
                    value = self.object_keys.get((attribute.schema, attribute.object_type, value)) or value
                elif attribute.type == "status":
                    value = self.status_ids.get((schema.name, value)) or value
                values.append({"value": value})
            if attribute.name in attr_ids and (values or only is not None):
                payload.append({"objectTypeAttributeId": attr_ids[attribute.name], "objectAttributeValues": values})
        return payload


def _normal(attribute: Attribute, value: str) -> str:
    if attribute.type == "float":
        try:
            return repr(float(value))
        except ValueError:
            return value
    return value.strip()


def run(client: AssetsClient, schemas: list[Schema], apply: bool) -> Result:
    return Provisioner(client, schemas, apply).run()


def referenced_schemas(schemas: list[Schema]) -> set[str]:
    """Schemas whose objects are referenced from another schema."""
    return {target for schema in schemas for target in schema.depends_on()}


def required_groups(schemas: list[Schema]) -> set[str]:
    """Every Jira group named in a Group attribute of the data files."""
    return {value for schema in schemas for object_type in schema.object_types
            for attribute in object_type.attributes if attribute.type == "group"
            for row in load_objects(object_type) for value in attribute.values(row.get(attribute.name, ""))}


def preflight(client: AssetsClient, schemas: list[Schema]) -> list[tuple[str, bool | None, str]]:
    """Read-only prerequisite checks: (check, passed (None = could not check), detail)."""
    checks = []
    try:
        existing = client.schemas()
        discovered = client.base.rstrip("/").split("/workspace/")[-1].split("/")[0]
        source = ("from ASSETS_WORKSPACE_ID" if client.config.workspace_id else
                  "set ASSETS_API_BASE" if client.config.api_base else
                  f"discovered via the JSM API; set ASSETS_WORKSPACE_ID={discovered} to skip that call")
        checks.append(("Jira Assets access", True, f"workspace reachable ({source}), {len(existing)} existing schema(s)"))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        return [("Jira Assets access", False, str(exc))]
    by_name = {s.get("name"): s for s in existing}
    for name in sorted(referenced_schemas(schemas)):
        current = by_name.get(name)
        if current is None:
            checks.append((f"Cross-schema references on '{name}'", True, "schema not created yet; requested at creation"))
        elif current.get("crossSchemaReferencingAllowed", True):
            checks.append((f"Cross-schema references on '{name}'", True, "allowed"))
        else:
            checks.append((f"Cross-schema references on '{name}'", False, CROSS_SCHEMA_HELP.split(": ", 1)[1]))
    by_key = {str(s.get("objectSchemaKey", "")).upper(): s for s in existing}
    for schema in schemas:
        clash = by_key.get(schema.key.upper())
        if clash and clash.get("name") != schema.name:
            checks.append((f"Schema key '{schema.key}'", False,
                           f"already used by schema '{clash.get('name')}': change `key:` in schemas/*.yaml"))
        else:
            checks.append((f"Schema key '{schema.key}'", True,
                           f"used by '{schema.name}' already" if clash else f"free for '{schema.name}'"))
    groups = required_groups(schemas)
    missing = client.missing_groups(groups)
    if missing is None:
        checks.append(("Jira groups", None, f"could not verify {sorted(groups)} (no group read access, or a non-Cloud base URL)"))
    else:
        for group in sorted(groups):
            checks.append((f"Jira group '{group}'", group not in missing,
                           "exists" if group not in missing else "MISSING: create it in admin.atlassian.com > Directory > Groups, or change it in data/*.csv"))
    return checks
