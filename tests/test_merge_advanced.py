"""Tests for the upgraded merge: explicit LLM supersession + embedding matching.

Run: python -m tests.test_merge_advanced
"""

from __future__ import annotations

import sys

from meetmind.merge import merge
from meetmind.schema import CandidateGraph, Graph, Node


def _cand(nodes):
    return CandidateGraph(nodes=nodes)


def test_explicit_supersession():
    """LLM says the new node supersedes an existing id -> trusted even if ids differ."""
    graph = Graph.empty()
    graph, _ = merge(graph, _cand([
        Node(id="decision-auth-jwt", type="Decision", title="Auth via JWT",
             content="Use JWT tokens.", source_meeting="m1"),
    ]), "m1", "t1")

    # completely different id/title, but explicit supersedes -> must supersede
    graph, log = merge(graph, _cand([
        Node(id="decision-auth-sessions", type="Decision", title="Auth via server sessions",
             content="Switch to server-side sessions.", source_meeting="m2",
             supersedes=["decision-auth-jwt"]),
    ]), "m2", "t2")

    old = graph.node_by_id("decision-auth-jwt")
    new = graph.node_by_id("decision-auth-sessions")
    assert old.status == "superseded", "explicit supersede must retire the old node"
    assert new.status == "active"
    assert "decision-auth-jwt" in new.supersedes
    assert graph.has_edge("decision-auth-sessions", "decision-auth-jwt", "SUPERSEDES")
    assert any("[explicit]" in l for l in log)
    print("explicit supersession: OK")


def test_embedding_match():
    """With a fake embedder, semantically-close nodes dedup even with unlike ids."""
    # fake embedder: same entity (cosine ~0.86, >= SAME_ENTITY_EMB) but NOT identical
    # (< IDENTICAL_EMB), so it must register as a CHANGE, not a duplicate.
    vocab = {
        "login": [1.0, 0.0, 0.0], "signin": [0.86, 0.51, 0.0],
        "billing": [0.0, 0.0, 1.0],
    }

    def fake_embed(texts):
        out = []
        for t in texts:
            tl = t.lower()
            key = "login" if "login" in tl else "signin" if "sign" in tl else "billing"
            out.append(vocab[key])
        return out

    graph = Graph.empty()
    graph, _ = merge(graph, _cand([
        Node(id="req-user-login", type="Requirement", title="User login",
             content="Users can login.", source_meeting="m1"),
    ]), "m1", "t1", embedder=fake_embed)

    # DIFFERENT concept-prefix id so the STRING matcher won't fire; only embeddings can.
    graph, log = merge(graph, _cand([
        Node(id="req-authentication-flow", type="Requirement", title="Sign-in with 2FA",
             content="Sign in now requires two-factor.", source_meeting="m2"),
    ]), "m2", "t2", embedder=fake_embed)

    active_reqs = [n for n in graph.nodes if n.type.value == "Requirement" and n.status == "active"]
    superseded = [n for n in graph.nodes if n.status == "superseded"]
    assert len(active_reqs) == 1, f"embedding should merge, not duplicate; got {len(active_reqs)} active"
    assert len(superseded) == 1, "old login req should be superseded via embedding match"
    assert any("~ CHANGED" in l for l in log)
    print("embedding match: OK")


def main():
    test_explicit_supersession()
    test_embedding_match()
    print("\nALL ADVANCED MERGE TESTS PASSED ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
