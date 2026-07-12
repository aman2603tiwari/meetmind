"""Human-in-the-loop review of low-confidence ('proposed') nodes.

When CG_CONFIDENCE_THRESHOLD is set, merge parks uncertain extractions as status
'proposed' (excluded from active state / export). A human then confirms or rejects.
Confirming applies any DEFERRED supersession recorded on the node.
"""

from __future__ import annotations

from typing import List

from .schema import Graph, Node


def proposed_nodes(graph: Graph) -> List[Node]:
    return [n for n in graph.nodes if n.status == "proposed"]


def confirm(graph: Graph, node_id: str) -> str:
    n = graph.node_by_id(node_id)
    if n is None:
        return f"no node {node_id}"
    if n.status != "proposed":
        return f"{node_id} is not proposed (status={n.status})"
    n.status = "active"
    # apply deferred supersession now
    for sid in list(n.supersedes):
        old = graph.node_by_id(sid)
        if old is not None and old.status == "active":
            old.status = "superseded"
            graph.add_edge(n.id, old.id, "SUPERSEDES")
    return f"confirmed {node_id} -> active"


def reject(graph: Graph, node_id: str) -> str:
    n = graph.node_by_id(node_id)
    if n is None:
        return f"no node {node_id}"
    if n.status != "proposed":
        return f"{node_id} is not proposed (status={n.status})"
    graph.nodes = [x for x in graph.nodes if x.id != node_id]
    graph.edges = [e for e in graph.edges if e.src != node_id and e.dst != node_id]
    return f"rejected {node_id} (removed)"


def confirm_all(graph: Graph) -> List[str]:
    return [confirm(graph, n.id) for n in list(proposed_nodes(graph))]


def reject_all(graph: Graph) -> List[str]:
    return [reject(graph, n.id) for n in list(proposed_nodes(graph))]
