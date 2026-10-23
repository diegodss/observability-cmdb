"""observability-cmdb command line.

    validate                 offline check of schemas/*.yaml and data/*.csv
    plan                     show what apply would change in Jira Assets (read-only)
    apply                    create/update schemas, types, attributes and objects
    lookup NAME [--failed-side source|target|self] [--json]
                             enrich one CI (resolver group, root CI, impact)
    serve [--host H] [--port P]
                             run the HTTP enrichment service
    initial-load SOT_CSV     regenerate data/*.csv from the Integrations SOT (once)
"""

import argparse
import json
import sys
from pathlib import Path

from . import initial_load, model, provision
from .assets import AssetsClient, AssetsError
from .config import Config
from .directory import JiraDirectory, MockDirectory
from .enrich import enrich


def _client(config: Config) -> AssetsClient:
    missing = config.missing_jira_settings()
    if missing:
        sys.exit(f"ERROR: set {', '.join(missing)} in .env (see .env.example)")
    return AssetsClient(config)


def _directory_factory(config: Config, schemas):
    if config.backend == "mock":
        directory = MockDirectory(schemas)
        return lambda: directory
    if config.backend != "jira":
        sys.exit(f"ERROR: CMDB_BACKEND must be 'jira' or 'mock', got {config.backend!r}")
    client = _client(config)
    return lambda: JiraDirectory(schemas, client)


def cmd_validate(_args, schemas, _config) -> int:
    problems = model.validate(schemas)
    for schema in schemas:
        for object_type in schema.object_types:
            print(f"  {schema.name}/{object_type.name}: {len(model.load_objects(object_type))} objects, "
                  f"{len(object_type.attributes)} attributes")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("Definitions are consistent" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_provision(args, schemas, config) -> int:
    problems = model.validate(schemas)
    if problems:
        print("Fix these before planning:\n  " + "\n  ".join(problems))
        return 1
    client = _client(config)
    apply = args.command == "apply"
    print(f"{'Applying to' if apply else 'Plan for'} {client.base}\n")
    result = provision.run(client, schemas, apply=apply)
    symbols = {"create": "+", "update": "~", "ok": " ", "drift": "!", "warn": "?"}
    for action in result.actions:
        if action.verb != "ok" or args.verbose:
            print(f"  {symbols[action.verb]} {action.verb:<6} {action.target}{f'  ({action.detail})' if action.detail else ''}")
    summary = ", ".join(f"{result.count(v)} {v}" for v in ("create", "update", "ok", "drift", "warn"))
    print(f"\n{summary}")
    if not apply and (result.count("create") or result.count("update")):
        print("Run `apply` to make these changes.")
    return 0


def cmd_lookup(args, schemas, config) -> int:
    result = enrich(_directory_factory(config, schemas)(), args.name, args.failed_side)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["found"] else 1
    if not result["found"]:
        print(f"CI '{args.name}' not found ({result['enrichment']['backend']} backend)")
        return 1
    ci, root = result["ci"], result["root_ci"]
    rows = [("CI", f"{ci['name']} ({ci['type']}, {ci['schema']}{', ' + ci['key'] if ci['key'] else ''})"),
            ("Status", ci["status"]),
            ("Root CI", root["name"] if root["name"] != ci["name"] else "itself"),
            ("Resolver group", result["resolver_group"]),
            ("Informed", ", ".join(result["informed_groups"]) or "-"),
            ("Impact", f"{result['impact']['count']} integration(s); {', '.join(result['impact']['domains']) or '-'}")]
    for key, value in result["details"].items():
        if isinstance(value, dict):
            value = f"{value['name']} (resolver: {value['resolver_group']})"
        elif isinstance(value, list):
            value = ", ".join(v["name"] for v in value) or "-"
        rows.append((key.replace("_", " ").capitalize().replace(" cis", " CIs"), value or "-"))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")
    if result["impact"]["integrations"]:
        print("\n  Affected integrations:")
        for row in result["impact"]["integrations"]:
            print(f"    - {row['name']}  [{row['direction']}, resolver: {row['resolver_group']}]")
    e = result["enrichment"]
    print(f"\n  ({e['backend']} backend, {e['lookups']} lookups)")
    return 0


def cmd_serve(args, schemas, config) -> int:
    import uvicorn
    from .server import create_app
    uvicorn.run(create_app(_directory_factory(config, schemas)), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_initial_load(args, schemas, _config) -> int:
    counts, problems = initial_load.run(Path(args.sot), schemas)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    print(f"Wrote {counts['applications']} applications, {counts['connectors']} connectors and "
          f"{counts['integrations']} integrations from {counts['sot_rows']} SOT rows")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cmdb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("-v", "--verbose", action="store_true", help="also list items that are already in place")
    p = sub.add_parser("lookup")
    p.add_argument("name")
    p.add_argument("--failed-side", choices=["source", "target", "self"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("initial-load")
    p.add_argument("sot")
    args = parser.parse_args()
    schemas = model.load_schemas()
    config = Config.from_env()
    handler = {"validate": cmd_validate, "plan": cmd_provision, "apply": cmd_provision, "lookup": cmd_lookup,
               "serve": cmd_serve, "initial-load": cmd_initial_load}[args.command]
    try:
        return handler(args, schemas, config)
    except AssetsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
