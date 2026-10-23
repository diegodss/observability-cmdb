"""Mermaid diagram of the CIs and their relationships, from the data files.

Drawn as data flows (source system -> integration -> target system), with
connectors as dotted links. Generated from data/*.csv so it matches what
`apply` loads into Jira Assets.
"""

from .model import Schema, load_objects

CLASS_STYLES = {
    "Application": "fill:#e8f1fb,stroke:#185fa5,color:#0c447c",
    "Integration": "fill:#e1f5ee,stroke:#0f6e56,color:#085041",
    "Connector": "fill:#f1efe8,stroke:#5f5e5a,color:#444441",
}


def _label(name: str, resolver: str) -> str:
    text = name.replace('"', "'")
    return f'"{text}<br/><small>resolver: {resolver}</small>"' if resolver else f'"{text}"'


def mermaid(schemas: list[Schema]) -> str:
    """Nodes coloured by object type; Mermaid lays the flows out left to right."""
    ids: dict[str, str] = {}
    lines = ["flowchart LR"]
    rows_by_type = {}
    for schema in schemas:
        for object_type in schema.object_types:
            rows_by_type[object_type.name] = load_objects(object_type)
    def node_line(row: dict, type_name: str, indent: str = "  ") -> str:
        node = ids.setdefault(row["Name"], f"n{len(ids)}")
        shape = ("([", "])") if type_name == "Connector" else ("[", "]")
        return f"{indent}{node}{shape[0]}{_label(row['Name'], row.get('Resolver Group', ''))}{shape[1]}:::{type_name.lower()}"

    for row in rows_by_type.get("Application", []):
        lines.append(node_line(row, "Application"))
    packages = {p["Name"]: p for p in rows_by_type.get("Integration Package", [])}
    integrations = rows_by_type.get("Integration", [])
    for package, members in sorted({p: [i for i in integrations if i.get("Package") == p] for p in packages}.items()):
        box = f"pkg_{len(lines)}"
        lines.append(f'  subgraph {box}["Package {package}"]')
        lines += [node_line(row, "Integration", "    ") for row in members]
        lines.append("  end")
        lines.append(f"  style {box} fill:#fafaf7,stroke:#888780,stroke-dasharray:4 3,color:#444441")
    for row in integrations:
        if row["Name"] not in ids:
            lines.append(node_line(row, "Integration"))
    for row in rows_by_type.get("Connector", []):
        lines.append(node_line(row, "Connector"))

    for row in rows_by_type.get("Integration", []):
        node = ids[row["Name"]]
        if row.get("Source System") in ids:
            lines.append(f"  {ids[row['Source System']]} -->|reads from| {node}")
        if row.get("Target System") in ids:
            lines.append(f"  {node} -->|writes to| {ids[row['Target System']]}")
        for connector in (c.strip() for c in row.get("Connector CIs", "").split("|") if c.strip()):
            if connector in ids:
                lines.append(f"  {node} -.-> {ids[connector]}")
    for row in rows_by_type.get("Connector", []):
        if row.get("Connects To") in ids:
            lines.append(f"  {ids[row['Name']]} -.->|connects to| {ids[row['Connects To']]}")
    lines += [f"  classDef {name.lower()} {style}" for name, style in CLASS_STYLES.items()]
    return "\n".join(lines) + "\n"
