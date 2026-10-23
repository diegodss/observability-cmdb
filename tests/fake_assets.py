"""In-memory fake of the Jira Assets REST API v1 endpoints that cmdb uses.

Shapes follow the Assets Cloud API closely enough to exercise plan/apply and the
Jira read path in tests. It is not a full emulation.
"""

import itertools
import re

from fastapi import FastAPI, HTTPException, Request


def create_fake() -> FastAPI:
    app = FastAPI()
    ids = itertools.count(1)
    state = {"schemas": {}, "types": {}, "attrs": {}, "statuses": {}, "reftypes": {}, "objects": {}}
    app.state.data = state
    app.state.max_description = 1000
    app.state.honour_cross_schema_flag = True
    icons = [{"id": "1", "name": "Application"}, {"id": "2", "name": "Integration"}, {"id": "3", "name": "Generic"}]

    def new_id() -> str:
        return str(next(ids))

    # -------------------------------------------------------------- structure
    @app.get("/objectschema/list")
    def schemas():
        return {"values": list(state["schemas"].values())}

    @app.post("/objectschema/create")
    async def create_schema(request: Request):
        body = await request.json()
        if len(body.get("description", "")) > app.state.max_description:  # mimics Jira's size limit
            raise HTTPException(400, {"errorMessages": [], "errors": {
                "description": "i18n.constraint.violation.ObjectSchemaBean.Size.description"}})
        schema = {"id": new_id(), "name": body["name"], "objectSchemaKey": body["objectSchemaKey"],
                  "crossSchemaReferencingAllowed": body.get("crossSchemaReferencingAllowed", False)
                  if app.state.honour_cross_schema_flag else False}
        state["schemas"][schema["id"]] = schema
        return schema

    @app.get("/objectschema/{schema_id}/objecttypes/flat")
    def object_types(schema_id: str):
        return [t for t in state["types"].values() if t["objectSchemaId"] == schema_id]

    @app.post("/objecttype/create")
    async def create_type(request: Request):
        body = await request.json()
        object_type = {"id": new_id(), "name": body["name"], "objectSchemaId": body["objectSchemaId"],
                       "iconId": body["iconId"]}
        state["types"][object_type["id"]] = object_type
        for name, label in (("Key", False), ("Name", True), ("Created", False), ("Updated", False)):
            attr_id = new_id()
            state["attrs"][attr_id] = {"id": attr_id, "objectTypeId": object_type["id"], "name": name,
                                       "label": label, "type": 0, "defaultType": {"id": 0}}
        return object_type

    @app.get("/objecttype/{type_id}/attributes")
    def attributes(type_id: str):
        return [a for a in state["attrs"].values() if a["objectTypeId"] == type_id]

    @app.post("/objecttypeattribute/{type_id}")
    async def create_attribute(type_id: str, request: Request):
        body = await request.json()
        if body["type"] == 1 and (not body.get("typeValue") or not body.get("additionalValue")):
            raise HTTPException(400, "reference attribute needs typeValue and additionalValue")
        attr_id = new_id()
        attribute = {"id": attr_id, "objectTypeId": type_id, "name": body["name"], "label": False,
                     "type": body["type"], "defaultType": {"id": body.get("defaultTypeId", -1)},
                     "typeValue": body.get("typeValue"), "maximumCardinality": body.get("maximumCardinality", 1)}
        state["attrs"][attr_id] = attribute
        return attribute

    @app.get("/config/statustype")
    def statuses(objectSchemaId: str):
        return [s for s in state["statuses"].values() if s["objectSchemaId"] == objectSchemaId]

    @app.post("/config/statustype")
    async def create_status(request: Request):
        body = await request.json()
        status = {"id": new_id(), **body}
        state["statuses"][status["id"]] = status
        return status

    @app.get("/config/referencetype")
    def reftypes(objectSchemaId: str):
        return [r for r in state["reftypes"].values() if r["objectSchemaId"] == objectSchemaId]

    @app.post("/config/referencetype")
    async def create_reftype(request: Request):
        body = await request.json()
        reftype = {"id": new_id(), **body}
        state["reftypes"][reftype["id"]] = reftype
        return reftype

    @app.get("/icon/global")
    def global_icons():
        return icons

    # -------------------------------------------------------------- objects
    def store_values(obj: dict, attributes: list[dict]) -> None:
        for item in attributes:
            attribute = state["attrs"][str(item["objectTypeAttributeId"])]
            values = []
            for value in (v["value"] for v in item["objectAttributeValues"]):
                if attribute["type"] == 1:
                    target = next((o for o in state["objects"].values() if o["objectKey"] == value), None)
                    if target is None:
                        raise HTTPException(400, f"no object with key {value}")
                    target_schema = state["schemas"][target["schemaId"]]
                    if target["schemaId"] != obj["schemaId"] and not target_schema["crossSchemaReferencingAllowed"]:
                        raise HTTPException(400, {"errors": {"x": f"refers to the schema {target_schema['name']} "
                                                             "but this schema has cross-schema relations disabled"}})
                    values.append({"referencedObject": {"label": target["label"], "objectKey": value}})
                elif attribute["type"] == 7:
                    values.append({"status": {"id": value, "name": state["statuses"][str(value)]["name"]}})
                elif attribute["type"] == 4:
                    values.append({"group": {"name": value}})
                else:
                    values.append({"value": value, "displayValue": value})
            obj["values"][attribute["id"]] = values
            if attribute["label"]:
                obj["label"] = values[0]["value"]

    @app.post("/object/create")
    async def create_object(request: Request):
        body = await request.json()
        object_type = state["types"][str(body["objectTypeId"])]
        schema = state["schemas"][object_type["objectSchemaId"]]
        number = sum(1 for o in state["objects"].values() if o["schemaId"] == schema["id"]) + 1
        obj = {"id": new_id(), "objectKey": f"{schema['objectSchemaKey']}-{number}", "label": "",
               "typeId": object_type["id"], "schemaId": schema["id"], "values": {}}
        store_values(obj, body["attributes"])
        state["objects"][obj["id"]] = obj
        return render(obj)

    @app.put("/object/{object_id}")
    async def update_object(object_id: str, request: Request):
        body = await request.json()
        obj = state["objects"][object_id]
        store_values(obj, body["attributes"])
        return render(obj)

    def render(obj: dict) -> dict:
        object_type = state["types"][obj["typeId"]]
        return {"id": obj["id"], "objectKey": obj["objectKey"], "label": obj["label"],
                "objectType": {"id": object_type["id"], "name": object_type["name"]},
                "attributes": [{"objectTypeAttributeId": attr_id, "objectAttributeValues": values}
                               for attr_id, values in obj["values"].items()]}

    def display(value: dict) -> str:
        for key, field in (("referencedObject", "label"), ("group", "name"), ("status", "name")):
            if key in value:
                return value[key][field]
        return str(value["value"])

    def matches(obj: dict, key: str, wanted: str) -> bool:
        object_type = state["types"][obj["typeId"]]
        if key == "objectSchema":
            return state["schemas"][obj["schemaId"]]["name"] == wanted
        if key == "objectType":
            return object_type["name"] == wanted
        attr = next((a for a in state["attrs"].values() if a["objectTypeId"] == obj["typeId"] and a["name"] == key), None)
        return attr is not None and wanted in [display(v) for v in obj["values"].get(attr["id"], [])]

    @app.post("/object/aql")
    async def aql(request: Request):
        query = (await request.json())["qlQuery"]
        conditions = [re.match(r'\s*"?([^"=]+?)"?\s*=\s*"(.*)"\s*$', part).groups() for part in query.split(" AND ")]
        found = [render(o) for o in state["objects"].values()
                 if all(matches(o, k, v.replace('\\"', '"')) for k, v in conditions)]
        return {"values": found, "total": len(found), "isLast": True}

    return app
