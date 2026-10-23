"""Thin client for the Jira Assets (Cloud) REST API v1.

Every endpoint the tool uses is in this file. Base URL:
https://api.atlassian.com/jsm/assets/workspace/{workspaceId}/v1
"""

import base64

import httpx

from .config import Config


class AssetsError(Exception):
    def __init__(self, status: int, method: str, path: str, detail: str):
        hint = {401: "check JIRA_EMAIL / JIRA_API_TOKEN", 403: "the account lacks Assets permissions for this action",
                404: "endpoint or object not found: check JIRA_SITE / workspace / ASSETS_API_BASE"}.get(status, "")
        super().__init__(f"HTTP {status} for {method} {path}{f' ({hint})' if hint else ''}: {detail[:300]}")
        self.status = status


class AssetsClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        raw = f"{config.jira_email}:{config.jira_api_token}".encode()
        headers = {"Authorization": "Basic " + base64.b64encode(raw).decode(), "Accept": "application/json"}
        self.http = http or httpx.Client(timeout=30, headers=headers)
        if http is not None:
            self.http.headers.update(headers)
        self.base = config.api_base or self._discover_base()
        self._attribute_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ plumbing
    def _discover_base(self) -> str:
        workspace = self.config.workspace_id
        if not workspace:
            values = self._call("GET", f"{self.config.jira_site}/rest/servicedeskapi/assets/workspace").get("values", [])
            if not values:
                raise AssetsError(404, "GET", "/rest/servicedeskapi/assets/workspace",
                                  "no Assets workspace (is Jira Service Management with Assets enabled?)")
            workspace = values[0]["workspaceId"]
        return f"https://api.atlassian.com/jsm/assets/workspace/{workspace}/v1"

    def _call(self, method: str, url: str, **kwargs):
        response = self.http.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise AssetsError(response.status_code, method, url.replace(self.base if hasattr(self, "base") else "", ""),
                              response.text)
        return response.json() if response.content else {}

    def get(self, path: str, **params):
        return self._call("GET", self.base + path, params=params or None)

    def post(self, path: str, body: dict):
        return self._call("POST", self.base + path, json=body)

    def put(self, path: str, body: dict):
        return self._call("PUT", self.base + path, json=body)

    @staticmethod
    def _values(data) -> list[dict]:
        return data if isinstance(data, list) else data.get("values", data.get("objectEntries", []))

    # ------------------------------------------------------------------ schema structure
    def schemas(self) -> list[dict]:
        return self._values(self.get("/objectschema/list"))

    def create_schema(self, name: str, key: str, description: str) -> dict:
        return self.post("/objectschema/create", {"name": name, "objectSchemaKey": key, "description": description})

    def object_types(self, schema_id: str) -> list[dict]:
        return self._values(self.get(f"/objectschema/{schema_id}/objecttypes/flat"))

    def create_object_type(self, schema_id: str, name: str, description: str, icon_id: str) -> dict:
        return self.post("/objecttype/create", {"name": name, "description": description, "iconId": icon_id,
                                                "objectSchemaId": schema_id})

    def attributes(self, object_type_id: str, refresh: bool = False) -> list[dict]:
        if refresh or object_type_id not in self._attribute_cache:
            self._attribute_cache[object_type_id] = self._values(self.get(f"/objecttype/{object_type_id}/attributes"))
        return self._attribute_cache[object_type_id]

    def create_attribute(self, object_type_id: str, body: dict) -> dict:
        self._attribute_cache.pop(object_type_id, None)
        return self.post(f"/objecttypeattribute/{object_type_id}", body)

    def status_types(self, schema_id: str) -> list[dict]:
        return self._values(self.get("/config/statustype", objectSchemaId=schema_id))

    def create_status_type(self, schema_id: str, name: str, category: int) -> dict:
        return self.post("/config/statustype", {"name": name, "category": category, "objectSchemaId": schema_id})

    def reference_types(self, schema_id: str) -> list[dict]:
        return self._values(self.get("/config/referencetype", objectSchemaId=schema_id))

    def create_reference_type(self, schema_id: str, name: str, color: str) -> dict:
        return self.post("/config/referencetype", {"name": name, "color": color, "objectSchemaId": schema_id})

    def icons(self) -> list[dict]:
        return self._values(self.get("/icon/global"))

    # ------------------------------------------------------------------ objects
    def aql(self, query: str) -> list[dict]:
        """Run an AQL query; return objects as {id, key, label, type, type_id, attrs{name: [display values]}}."""
        results, start = [], 0
        while True:
            data = self._call("POST", f"{self.base}/object/aql",
                              params={"startAt": start, "maxResults": 100, "includeAttributes": "true"},
                              json={"qlQuery": query})
            entries = self._values(data)
            results.extend(self._parse(entry) for entry in entries)
            if data.get("isLast", True) or not entries or isinstance(data, list):
                return results
            start += len(entries)

    def create_object(self, object_type_id: str, attributes: list[dict]) -> dict:
        return self.post("/object/create", {"objectTypeId": object_type_id, "attributes": attributes})

    def update_object(self, object_id: str, object_type_id: str, attributes: list[dict]) -> dict:
        return self.put(f"/object/{object_id}", {"objectTypeId": object_type_id, "attributes": attributes})

    def _parse(self, entry: dict) -> dict:
        object_type = entry.get("objectType", {})
        type_id = str(object_type.get("id", ""))
        names = None
        attrs: dict[str, list[str]] = {}
        for attr in entry.get("attributes", []):
            name = (attr.get("objectTypeAttribute") or {}).get("name")
            if not name:
                names = names or {str(a["id"]): a["name"] for a in self.attributes(type_id)}
                name = names.get(str(attr.get("objectTypeAttributeId")), f"attribute#{attr.get('objectTypeAttributeId')}")
            attrs[name] = [display_value(v) for v in attr.get("objectAttributeValues", [])]
        return {"id": str(entry.get("id", "")), "key": entry.get("objectKey", ""), "label": entry.get("label", ""),
                "type": object_type.get("name", ""), "type_id": type_id, "attrs": attrs}

    # ------------------------------------------------------------------ Jira groups
    def missing_groups(self, names: set[str]) -> list[str] | None:
        """Group names that don't exist in Jira; None when the check can't be made."""
        if not self.config.jira_site or self.config.api_base:
            return None
        missing = []
        for name in sorted(names):
            try:
                found = self._call("GET", f"{self.config.jira_site}/rest/api/3/group/bulk",
                                   params={"groupName": name}).get("values", [])
            except AssetsError:
                return None
            if not any(g.get("name") == name for g in found):
                missing.append(name)
        return missing


def display_value(value: dict) -> str:
    for key, field in (("referencedObject", "label"), ("group", "name"), ("user", "displayName"), ("status", "name")):
        nested = value.get(key)
        if isinstance(nested, dict) and nested.get(field):
            return str(nested[field])
    return str(value.get("displayValue") or value.get("value") or "")
