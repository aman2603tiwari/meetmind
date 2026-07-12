"""Offline end-to-end test of the temporal merge — no Groq/API key needed.

Simulates two meetings by hand-building CandidateGraphs, then asserts the
supersession behavior that is the whole point of the system.

Run: python -m tests.test_merge_offline   (from repo root)
"""

from __future__ import annotations

import sys

from meetmind.merge import merge
from meetmind.schema import CandidateGraph, Edge, Graph, Node


def _cand(nodes, edges=None) -> CandidateGraph:
    return CandidateGraph(nodes=nodes, edges=edges or [])


def main() -> int:
    graph = Graph.empty()

    # --- meeting 1: REST + Postgres + idempotency + open refunds question ---
    m1 = _cand(
        nodes=[
            Node(id="decision-api-rest", type="Decision", title="API uses REST",
                 content="Public API is REST for v1.", source_meeting="m1"),
            Node(id="decision-store-postgres", type="Decision", title="Postgres datastore",
                 content="Use Postgres as the main datastore for transactions.", source_meeting="m1"),
            Node(id="req-idempotent-payments", type="Requirement", title="Idempotent payments",
                 content="Payment creation must be idempotent; clients retry.", source_meeting="m1"),
            Node(id="q-refunds", type="OpenQuestion", title="Refunds in v1?",
                 content="Undecided whether refunds ship in v1.", source_meeting="m1"),
        ],
    )
    graph, log1 = merge(graph, m1, "m1", "2026-07-12T00:00:00+00:00")
    print("meeting 1:")
    for l in log1:
        print("  " + l)

    assert len(graph.active_nodes()) == 4, "m1 should create 4 active nodes"

    # --- meeting 2: REST -> GraphQL (change), refunds resolved, rest unchanged ---
    m2 = _cand(
        nodes=[
            Node(id="decision-api-graphql", type="Decision", title="API uses GraphQL",
                 content="Switch the public API from REST to GraphQL for flexible queries.",
                 source_meeting="m2"),
            Node(id="decision-store-postgres", type="Decision", title="Postgres datastore",
                 content="Use Postgres as the main datastore for transactions.", source_meeting="m2"),
            Node(id="req-idempotent-payments", type="Requirement", title="Idempotent payments",
                 content="Payment creation must be idempotent; clients retry.", source_meeting="m2"),
            Node(id="req-refunds-v1", type="Requirement", title="Refund support in v1",
                 content="Refunds are now a v1 requirement.", source_meeting="m2"),
        ],
    )
    graph, log2 = merge(graph, m2, "m2", "2026-07-19T00:00:00+00:00")
    print("meeting 2:")
    for l in log2:
        print("  " + l)

    # --- assertions: the core behavior ---
    rest = graph.node_by_id("decision-api-rest")
    graphql = next(n for n in graph.nodes if n.type.value == "Decision" and "GraphQL" in n.title)

    assert rest.status == "superseded", "REST decision must be superseded"
    assert graphql.status == "active", "GraphQL decision must be active"
    assert "decision-api-rest" in graphql.supersedes, "GraphQL must supersede REST"
    assert graph.has_edge(graphql.id, "decision-api-rest", "SUPERSEDES"), "SUPERSEDES edge missing"

    # no duplicate Postgres / idempotency nodes
    postgres = [n for n in graph.nodes if n.id.startswith("decision-store-postgres")]
    assert len(postgres) == 1, f"Postgres should not duplicate, got {len(postgres)}"
    idem = [n for n in graph.nodes if n.id.startswith("req-idempotent-payments")]
    assert len(idem) == 1, f"idempotency should not duplicate, got {len(idem)}"

    # refunds: a brand-new active requirement now exists
    assert any(n.status == "active" and "efund" in n.title for n in graph.nodes), "refund req missing"

    active = {n.id for n in graph.active_nodes()}
    print("\nactive nodes:", sorted(active))
    assert "decision-api-rest" not in active
    assert graphql.id in active

    print("\nALL ASSERTIONS PASSED ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
