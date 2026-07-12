"""Export the ACTIVE state of the graph as a clean spec for the coding agent.

The full temporal graph carries history (superseded nodes, provenance edges) that
a coding agent shouldn't wade through. This produces the "current truth" only,
grouped by Feature, as Markdown (human/agent readable) or JSON (programmatic).
"""

from __future__ import annotations

import json
from typing import Dict, List

from .schema import Graph, Node

# order in which node types appear under each feature
_TYPE_ORDER = [
    "Requirement", "Decision", "Constraint", "Interface",
    "Component", "ActionItem", "OpenQuestion",
]


def _features(graph: Graph) -> List[Node]:
    return [n for n in graph.active_nodes() if n.type.value == "Feature"]


def _implementers(graph: Graph, feature_id: str) -> set:
    """Active node ids linked to a feature via IMPLEMENTS (either direction)."""
    ids = set()
    for e in graph.edges:
        if e.type != "IMPLEMENTS":
            continue
        if e.dst == feature_id:
            ids.add(e.src)
        elif e.src == feature_id:
            ids.add(e.dst)
    return ids


def _group(graph: Graph) -> Dict[str, List[Node]]:
    """Group active non-Feature nodes by feature title; unlinked -> 'General'."""
    active = {n.id: n for n in graph.active_nodes()}
    groups: Dict[str, List[Node]] = {}
    claimed = set()

    for feat in _features(graph):
        members = [active[i] for i in _implementers(graph, feat.id) if i in active]
        groups[feat.title] = members
        claimed.update(n.id for n in members)

    leftovers = [n for n in graph.active_nodes()
                 if n.type.value != "Feature" and n.id not in claimed]
    if leftovers:
        groups.setdefault("General", []).extend(leftovers)
    return groups


def _sort_key(n: Node):
    order = _TYPE_ORDER.index(n.type.value) if n.type.value in _TYPE_ORDER else len(_TYPE_ORDER)
    return (order, n.title.lower())


def _cross_project_deps(graph: Graph) -> Dict[str, list]:
    """node_id -> [(type, 'slug:id')] for edges pointing at another project."""
    out: Dict[str, list] = {}
    for e in graph.edges:
        if ":" in e.dst and e.type != "DECIDED_IN":
            out.setdefault(e.src, []).append((e.type, e.dst))
    return out


def to_markdown(graph: Graph) -> str:
    project = graph.meta.project or "Context"
    lines = [f"# {project} — current spec",
             "",
             f"_Active state after meetings: {', '.join(graph.meta.meetings) or '(none)'}._",
             ""]
    groups = _group(graph)
    deps = _cross_project_deps(graph)
    if not groups:
        lines.append("_(no active nodes)_")
        return "\n".join(lines)

    for feature, members in groups.items():
        lines.append(f"## {feature}")
        if not members:
            lines.append("_(no active items yet)_\n")
            continue
        for n in sorted(members, key=_sort_key):
            owner = f" — _owner: {n.owner}_" if n.owner else ""
            lines.append(f"- **[{n.type.value}]** {n.title}{owner}")
            lines.append(f"  - {n.content}")
            for etype, ref in deps.get(n.id, []):
                lines.append(f"  - 🔗 {etype.lower().replace('_', ' ')} → `{ref}` _(other project)_")
            if n.type.value == "OpenQuestion":
                lines.append("  - ⚠️ UNRESOLVED — do not assume an answer.")
        lines.append("")
    return "\n".join(lines)


def to_json(graph: Graph) -> str:
    groups = _group(graph)
    deps = _cross_project_deps(graph)

    def node_obj(n: Node) -> dict:
        obj = {
            "id": n.id, "type": n.type.value, "title": n.title,
            "content": n.content, "owner": n.owner,
            "source_meeting": n.source_meeting,
        }
        if n.id in deps:
            obj["cross_project_deps"] = [{"type": t, "ref": r} for t, r in deps[n.id]]
        return obj

    out = {
        "project": graph.meta.project or None,
        "meetings": graph.meta.meetings,
        "features": {
            feature: [node_obj(n) for n in sorted(members, key=_sort_key)]
            for feature, members in groups.items()
        },
    }
    return json.dumps(out, ensure_ascii=False, indent=2)
