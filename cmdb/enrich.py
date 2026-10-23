"""Enrich one CI: its resolver group, the root CI, who to inform, and the impact.

This is what Keep calls for each alert, with the alert's `ci_name` label.

Root CI rules:
  Application  -> itself; impact = integrations that read from or write to it
  Connector    -> itself; impact = integrations that use it
  Integration Package -> itself; impact = its member integrations
  Integration  -> itself, unless `failed_side` says the failure is on its
                  `source` or `target` system; then that application, and the
                  impact is every integration of that application
"""

from datetime import datetime, timezone

from .directory import Directory
from .model import load_routing, route

FAILED_SIDES = {None, "self", "source", "target"}


def _first(ci: dict, attribute: str) -> str:
    values = ci["attrs"].get(attribute, [])
    return values[0] if values else ""


def _summary(ci: dict) -> dict:
    return {"name": ci["name"], "type": ci["type"], "schema": ci["schema"], "key": ci["key"],
            "status": _first(ci, "Status"), "resolver_group": _first(ci, "Resolver Group")}


def _integration_row(ci: dict, direction: str) -> dict:
    return {"name": ci["name"], "direction": direction, "resolver_group": _first(ci, "Resolver Group"),
            "status": _first(ci, "Status"), "domain": _first(ci, "Domain"), "subdomain": _first(ci, "Subdomain"),
            "source_system": _first(ci, "Source System"), "target_system": _first(ci, "Target System")}


def _application_impact(directory: Directory, name: str) -> list[dict]:
    rows = {}
    for attribute, direction in (("Target System", "writes to"), ("Source System", "reads from")):
        for ci in directory.referencing("Integration", attribute, name):
            rows.setdefault(ci["name"], _integration_row(ci, direction))
    return sorted(rows.values(), key=lambda r: r["name"])


def enrich(directory: Directory, name: str, failed_side: str | None = None) -> dict:
    if failed_side not in FAILED_SIDES:
        raise ValueError(f"failed_side must be one of source, target, self; got {failed_side!r}")
    started = directory.lookups
    ci = directory.get(name)
    meta = {"backend": directory.backend, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if ci is None:
        return {"found": False, "ci_name": name,
                "enrichment": {**meta, "lookups": directory.lookups - started,
                               "note": "CI not found in the CMDB; route to the default group"}}

    details: dict = {}
    root = ci
    if ci["type"] == "Integration":
        source = directory.get(_first(ci, "Source System"))
        target = directory.get(_first(ci, "Target System"))
        connectors = [c for c in (directory.get(n) for n in ci["attrs"].get("Connector CIs", [])) if c]
        details = {"package": _first(ci, "Package"),
                   "domain": _first(ci, "Domain"), "subdomain": _first(ci, "Subdomain"),
                   "source_system": _summary(source) if source else None,
                   "target_system": _summary(target) if target else None,
                   "platform": _first(ci, "Platform"), "schedule": _first(ci, "Schedule"),
                   "connectors": _first(ci, "Connectors"),
                   "connector_cis": [_summary(c) for c in connectors],
                   "log_source": _first(ci, "Log Source")}
        if failed_side == "source" and source:
            root = source
        elif failed_side == "target" and target:
            root = target
        impact = _application_impact(directory, root["name"]) if root is not ci else [_integration_row(ci, "self")]
    elif ci["type"] == "Integration Package":
        details = {"source_systems": ci["attrs"].get("Source System", []),
                   "target_systems": ci["attrs"].get("Target System", []),
                   "connector_cis": ci["attrs"].get("Connector CIs", [])}
        impact = [_integration_row(c, "member") for c in directory.referencing("Integration", "Package", name)]
    elif ci["type"] == "Connector":
        details = {"connects_to": _first(ci, "Connects To"), "platform": _first(ci, "Platform")}
        impact = [_integration_row(c, "uses") for c in directory.referencing("Integration", "Connector CIs", name)]
    else:  # Application
        details = {"aliases": _first(ci, "Aliases"), "hosting": _first(ci, "Hosting")}
        impact = _application_impact(directory, name)

    resolver = _first(root, "Resolver Group")
    informed = sorted(({r["resolver_group"] for r in impact} | {_first(ci, "Resolver Group")}) - {resolver, ""})
    return {
        "found": True,
        "ci": _summary(ci),
        "root_ci": _summary(root),
        "resolver_group": resolver,
        "informed_groups": informed,
        "routing": route(resolver, load_routing()),
        "impact": {"integrations": impact, "count": len(impact),
                   "domains": sorted({f"{r['domain']} / {r['subdomain']}".strip(" /") for r in impact} - {""})},
        "details": details,
        "enrichment": {**meta, "lookups": directory.lookups - started},
    }
