"""Read access to CIs, from Jira Assets or from the local data files (mock)."""

from .assets import AssetsClient
from .model import Schema, load_objects


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Directory:
    """CI = {name, type, schema, key, attrs{attribute: [values]}}."""
    backend = "abstract"

    def __init__(self, schemas: list[Schema]):
        self.schemas = schemas
        self.lookups = 0

    def get(self, name: str) -> dict | None:
        raise NotImplementedError

    def referencing(self, object_type: str, attribute: str, target: str) -> list[dict]:
        """CIs of `object_type` whose reference `attribute` points at the CI named `target`."""
        raise NotImplementedError

    def _types(self):
        for schema in self.schemas:
            for object_type in schema.object_types:
                yield schema, object_type


class MockDirectory(Directory):
    backend = "mock"

    def __init__(self, schemas: list[Schema]):
        super().__init__(schemas)
        self.cis = []
        for schema, object_type in self._types():
            for row in load_objects(object_type):
                attrs = {a.name: a.values(row.get(a.name, "")) for a in object_type.attributes if a.type != "label"}
                self.cis.append({"name": row["Name"], "type": object_type.name, "schema": schema.name, "key": "", "attrs": attrs})

    def get(self, name: str) -> dict | None:
        self.lookups += 1
        return next((ci for ci in self.cis if ci["name"] == name), None)

    def referencing(self, object_type: str, attribute: str, target: str) -> list[dict]:
        self.lookups += 1
        return [ci for ci in self.cis if ci["type"] == object_type and target in ci["attrs"].get(attribute, [])]


class JiraDirectory(Directory):
    backend = "jira"

    def __init__(self, schemas: list[Schema], client: AssetsClient):
        super().__init__(schemas)
        self.client = client

    def _ci(self, schema: str, obj: dict) -> dict:
        return {"name": obj["label"], "type": obj["type"], "schema": schema, "key": obj["key"], "attrs": obj["attrs"]}

    def get(self, name: str) -> dict | None:
        for schema, object_type in self._types():
            self.lookups += 1
            found = self.client.aql(f"objectSchema = {_quote(schema.name)} AND objectType = {_quote(object_type.name)} "
                                    f"AND Name = {_quote(name)}")
            if found:
                return self._ci(schema.name, found[0])
        return None

    def referencing(self, object_type: str, attribute: str, target: str) -> list[dict]:
        schema = next(s for s, t in self._types() if t.name == object_type)
        self.lookups += 1
        found = self.client.aql(f"objectSchema = {_quote(schema.name)} AND objectType = {_quote(object_type)} "
                                f"AND {_quote(attribute)} = {_quote(target)}")
        return [self._ci(schema.name, o) for o in found]
