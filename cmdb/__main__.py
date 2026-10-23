"""observability-cmdb command line.

    validate                 offline check of schemas/*.yaml and data/*.csv
    preflight                check prerequisites in Jira: access, resolver groups (read-only)
    plan                     prerequisites + what apply would change in Jira Assets (read-only)
    apply [--yes]            check prerequisites, show the plan, confirm, then create/update
    lookup NAME [--failed-side source|target|self] [--json]
                             enrich one CI (resolver group, root CI, impact)
    serve [--host H] [--port P]
                             run the HTTP enrichment service
    initial-load SOT_CSV     regenerate data/*.csv from the Integrations SOT (once)
    diagram                  print a Mermaid diagram of the CIs and relationships (from data/*.csv)
"""

import argparse
import json
import sys
from pathlib import Path

from . import diagram, initial_load, model, provision
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


def _print_preflight(checks, skip_groups: bool) -> bool:
    """Print the checks; True when nothing blocks an apply."""
    print("Prerequisites")
    ok = True
    for name, passed, detail in checks:
        mark = {True: "✓", False: "✗", None: "?"}[passed]
        print(f"  {mark} {name}: {detail}")
        if passed is False or (passed is None and not skip_groups):
            ok = False
    print()
    return ok


def _print_result(result, verbose: bool) -> None:
    symbols = {"create": "+", "update": "~", "ok": " ", "drift": "!", "warn": "?", "blocked": "✗"}
    for action in result.actions:
        if action.verb != "ok" or verbose:
            print(f"  {symbols[action.verb]} {action.verb:<6} {action.target}{f'  ({action.detail})' if action.detail else ''}")
    print("\n" + ", ".join(f"{result.count(v)} {v}" for v in ("create", "update", "ok", "drift", "warn", "blocked")))


def cmd_preflight(args, schemas, config) -> int:
    client = _client(config)
    print(f"Checking {config.jira_site or client.base}\n")
    ok = _print_preflight(provision.preflight(client, schemas), skip_groups=False)
    print("Ready for `apply`." if ok else "Not ready: fix the items marked ✗ (or ?) first.")
    return 0 if ok else 1


def cmd_provision(args, schemas, config) -> int:
    problems = model.validate(schemas)
    if problems:
        print("Fix these before planning:\n  " + "\n  ".join(problems))
        return 1
    client = _client(config)
    apply = args.command == "apply"
    target = config.jira_site or client.base
    print(f"{'Apply to' if apply else 'Plan for'} {target}\n")
    ready = _print_preflight(provision.preflight(client, schemas), skip_groups=args.skip_group_check)
    plan = provision.run(client, schemas, apply=False)
    _print_result(plan, args.verbose)
    changes = plan.count("create") + plan.count("update")
    if not apply:
        if changes:
            print("Run `apply` to make these changes." if ready else "Fix the prerequisites above, then run `apply`.")
        return 0 if ready else 1
    if not ready:
        print("\nNot applying: prerequisites failed (see above). Nothing was changed.")
        return 1
    if not changes:
        print("\nNothing to apply.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nNot applying: no terminal to confirm. Re-run with --yes to apply without a prompt.")
            return 1
        answer = input(f"\nApply {changes} change(s) to {target}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1
    print("\nApplying...\n")
    result = provision.run(client, schemas, apply=True)
    _print_result(result, args.verbose)
    if result.count("blocked"):
        print("\nStopped before creating objects: fix the items marked ✗ blocked, then re-run apply.")
        return 1
    print("\nDone. Run `plan` again: it should report 0 create, 0 update.")
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
            value = ", ".join(v["name"] if isinstance(v, dict) else v for v in value) or "-"
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
    if not config.api_key:
        print("WARNING: CMDB_API_KEY is not set: /ci requests are not authenticated (fine for local use only).")
    app = create_app(_directory_factory(config, schemas), api_key=config.api_key, cache_seconds=config.cache_seconds)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_diagram(_args, schemas, _config) -> int:
    print(diagram.mermaid(schemas), end="")
    return 0


def cmd_initial_load(args, schemas, _config) -> int:
    counts, problems = initial_load.run(Path(args.sot), schemas)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    print(f"Wrote {counts['applications']} applications, {counts['connectors']} connectors, "
          f"{counts['packages']} integration package(s) and {counts['integrations']} integrations "
          f"from {counts['sot_rows']} SOT rows")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cmdb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("preflight")
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("-v", "--verbose", action="store_true", help="also list items that are already in place")
        p.add_argument("--skip-group-check", action="store_true",
                       help="proceed when group existence can't be verified (not when groups are missing)")
        if name == "apply":
            p.add_argument("-y", "--yes", action="store_true", help="apply without the confirmation prompt")
    p = sub.add_parser("lookup")
    p.add_argument("name")
    p.add_argument("--failed-side", choices=["source", "target", "self"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("initial-load")
    p.add_argument("sot")
    sub.add_parser("diagram")
    args = parser.parse_args()
    schemas = model.load_schemas()
    config = Config.from_env()
    handler = {"validate": cmd_validate, "preflight": cmd_preflight, "plan": cmd_provision, "apply": cmd_provision, "lookup": cmd_lookup,
               "serve": cmd_serve, "initial-load": cmd_initial_load, "diagram": cmd_diagram}[args.command]
    try:
        return handler(args, schemas, config)
    except AssetsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
