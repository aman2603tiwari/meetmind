"""Graph integrity checks — protect the coding agent from acting on a broken graph.

Returns a list of Issue(severity, message). `error` = the graph is inconsistent;
`warning` = suspicious but usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .merge import _concept  # reuse the concept-prefix helper
from .schema import Graph


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        mark = "✖" if self.severity == "error" else "⚠"
        return f"{mark} [{self.severity}] {self.message}"


def validate(graph: Graph) -> List[Issue]:
    issues: List[Issue] = []
    ids = {n.id for n in graph.nodes}

    # duplicate node ids
    seen = set()
    for n in graph.nodes:
        if n.id in seen:
            issues.append(Issue("error", f"duplicate node id: {n.id}"))
        seen.add(n.id)

    # dangling edges (cross-project refs "slug:id" and meeting refs are allowed external targets)
    def _external(endpoint: str) -> bool:
        return ":" in endpoint

    for e in graph.edges:
        if e.src not in ids and not _looks_like_meeting(e) and not _external(e.src):
            issues.append(Issue("error", f"edge from unknown node: {e.src} -[{e.type}]-> {e.dst}"))
        if e.dst not in ids and not _looks_like_meeting(e) and not _external(e.dst):
            issues.append(Issue("error", f"edge to unknown node: {e.src} -[{e.type}]-> {e.dst}"))

    # supersedes references must exist
    for n in graph.nodes:
        for sid in n.supersedes:
            if sid not in ids:
                issues.append(Issue("error", f"{n.id} supersedes missing node {sid}"))

    # a superseded node should not itself be active
    for n in graph.nodes:
        for sid in n.supersedes:
            target = graph.node_by_id(sid)
            if target is not None and target.status == "active" and n.status == "active":
                issues.append(Issue(
                    "error",
                    f"{n.id} (active) supersedes {sid} but {sid} is still active",
                ))

    # SUPERSEDES cycles
    for cycle in _supersedes_cycles(graph):
        issues.append(Issue("error", "SUPERSEDES cycle: " + " -> ".join(cycle)))

    # two active nodes of the same type sharing a concept prefix (likely dup)
    by_key: dict = {}
    for n in graph.active_nodes():
        key = (n.type.value, _concept(n.id))
        by_key.setdefault(key, []).append(n.id)
    for (type_, concept), members in by_key.items():
        if concept and len(members) > 1:
            issues.append(Issue(
                "warning",
                f"{len(members)} active {type_} nodes share concept '{concept}': "
                + ", ".join(members) + " (possible duplicate)",
            ))

    return issues


def _looks_like_meeting(edge) -> bool:
    # DECIDED_IN edges point at a meeting id which is not a node — allowed.
    return edge.type == "DECIDED_IN"


def _supersedes_cycles(graph: Graph) -> List[List[str]]:
    adj = {n.id: list(n.supersedes) for n in graph.nodes}
    cycles: List[List[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in adj}

    def dfs(u, stack):
        color[u] = GREY
        stack.append(u)
        for v in adj.get(u, []):
            if v not in color:
                continue
            if color[v] == GREY:
                idx = stack.index(v)
                cycles.append(stack[idx:] + [v])
            elif color[v] == WHITE:
                dfs(v, stack)
        stack.pop()
        color[u] = BLACK

    for nid in list(adj):
        if color[nid] == WHITE:
            dfs(nid, [])
    return cycles
