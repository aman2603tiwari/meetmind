"""Merge a freshly-extracted CandidateGraph into the persistent Graph.

This is the heart of the system: it decides, for each candidate node, whether it
is NEW, a DUPLICATE (skip), or a CHANGE (supersede the old one). Getting this
right is what makes the graph *temporal* instead of a pile of duplicates.

v1 matching is intentionally dependency-free: normalized-id + token-overlap
similarity, scoped to the same node type. Upgrade to embeddings later if the
string heuristic mis-matches (see plan: out-of-scope for v1).
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from .embeddings import cosine
from .schema import CandidateGraph, Graph, Node

# similarity thresholds
SAME_ENTITY = 0.55      # >= this (string) ⇒ candidate refers to an existing node
SAME_ENTITY_EMB = 0.80  # >= this (embedding cosine) ⇒ same entity
IDENTICAL = 0.92        # >= this on content ⇒ nothing changed, skip
IDENTICAL_EMB = 0.97

_WORD = re.compile(r"[a-z0-9]+")


def _node_text(n: Node) -> str:
    return f"{n.type.value}: {n.title}. {n.content}"


def _tokens(text: str) -> set:
    return set(_WORD.findall(text.lower()))


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _id_parts(node_id: str) -> list:
    return [p for p in re.split(r"[-_]", node_id.lower()) if p]


def _id_stub(node_id: str) -> set:
    # "decision-api-graphql" -> {decision, api, graphql}; ids encode the concept
    return set(_id_parts(node_id))


def _concept(node_id: str) -> str:
    """The id with its final value-segment dropped: the SUBJECT the node is about.

    'decision-api-rest' and 'decision-api-graphql' both -> 'decision-api', so a
    changed value on the same subject reads as the same entity (a supersession),
    not a brand-new node. The extractor is prompted to keep concept prefixes
    stable across meetings for exactly this reason.
    """
    parts = _id_parts(node_id)
    return "-".join(parts[:-1]) if len(parts) > 1 else (parts[0] if parts else "")


def _string_entity_similarity(a: Node, b: Node) -> float:
    """String-only: how likely a and b are the SAME concept."""
    id_sim = _jaccard(_id_stub(a.id), _id_stub(b.id))
    title_sim = _jaccard(_tokens(a.title), _tokens(b.title))
    concept_sim = 0.85 if _concept(a.id) and _concept(a.id) == _concept(b.id) else 0.0
    return max(id_sim, title_sim, concept_sim)


def _content_similarity(a: Node, b: Node) -> float:
    return _jaccard(_tokens(a.content), _tokens(b.content))


def _is_same_entity(cand: Node, existing: Node, emb: Optional[Dict[str, list]]) -> bool:
    """Decide same-concept using string signal OR embedding cosine when available."""
    if _string_entity_similarity(cand, existing) >= SAME_ENTITY:
        return True
    if emb is not None and cand.id in emb and existing.id in emb:
        if cosine(emb[cand.id], emb[existing.id]) >= SAME_ENTITY_EMB:
            return True
    return False


def _is_identical(cand: Node, existing: Node, emb: Optional[Dict[str, list]]) -> bool:
    if _content_similarity(cand, existing) >= IDENTICAL:
        return True
    if emb is not None and cand.id in emb and existing.id in emb:
        if cosine(emb[cand.id], emb[existing.id]) >= IDENTICAL_EMB:
            return True
    return False


def _best_match(
    candidate: Node, graph: Graph, emb: Optional[Dict[str, list]]
) -> Tuple[Optional[Node], bool]:
    """Return (best same-type active node judged the same entity, is_same)."""
    best: Optional[Node] = None
    best_score = -1.0
    for existing in graph.nodes:
        if existing.type != candidate.type or existing.status != "active":
            continue
        # rank by the strongest available signal
        score = _string_entity_similarity(candidate, existing)
        if emb is not None and candidate.id in emb and existing.id in emb:
            score = max(score, cosine(emb[candidate.id], emb[existing.id]))
        if score > best_score:
            best, best_score = existing, score
    if best is not None and _is_same_entity(candidate, best, emb):
        return best, True
    return best, False


def _unique_id(desired: str, graph: Graph) -> str:
    if graph.node_by_id(desired) is None:
        return desired
    i = 2
    while graph.node_by_id(f"{desired}-v{i}") is not None:
        i += 1
    return f"{desired}-v{i}"


def _build_embeddings(
    candidate: CandidateGraph, graph: Graph, embedder: Optional[Callable]
) -> Optional[Dict[str, list]]:
    """Embed all candidate + existing-active nodes in one batch. None if no embedder."""
    if embedder is None:
        return None
    active = [n for n in graph.nodes if n.status == "active"]
    items = [(n.id, _node_text(n)) for n in active]
    items += [(n.id, _node_text(n)) for n in candidate.nodes]
    if not items:
        return {}
    try:
        vecs = embedder([t for _, t in items])
    except Exception:
        return None  # embedding failed → fall back to string matching
    return {nid: v for (nid, _), v in zip(items, vecs)}


def _supersede(graph: Graph, cand: Node, old: Node, meeting_id: str) -> None:
    old.status = "superseded"
    if old.id not in cand.supersedes:
        cand.supersedes.append(old.id)
    graph.add_edge(cand.id, old.id, "SUPERSEDES")


def merge(
    graph: Graph,
    candidate: CandidateGraph,
    meeting_id: str,
    now: str,
    embedder: Optional[Callable] = None,
    confidence_threshold: float = 0.0,
    id_remap_out: Optional[dict] = None,
) -> Tuple[Graph, List[str]]:
    """Merge candidate into graph in place. Returns (graph, human-readable changelog).

    Resolution order per candidate node:
      1. EXPLICIT supersedes from the LLM (references to existing active ids) — trusted.
      2. Otherwise similarity match (embedding cosine when available, else string).

    confidence_threshold > 0 routes low-confidence nodes to status 'proposed' (not
    active, not applied) for human review; their supersessions are DEFERRED until
    confirmed (see review.confirm).
    """
    changelog: List[str] = []
    id_remap: dict[str, str] = {}

    if meeting_id not in graph.meta.meetings:
        graph.meta.meetings.append(meeting_id)

    emb = _build_embeddings(candidate, graph, embedder)

    for cand in candidate.nodes:
        orig_id = cand.id
        cand.source_meeting = meeting_id
        cand.created_at = now
        proposed = cand.confidence < confidence_threshold
        tag = " [proposed]" if proposed else ""

        # (1) explicit supersession from the extractor
        explicit = [
            graph.node_by_id(sid) for sid in cand.supersedes
            if graph.node_by_id(sid) is not None and graph.node_by_id(sid).status == "active"
        ]
        if explicit:
            new_id = _unique_id(orig_id, graph)
            cand.id = new_id
            cand.supersedes = []
            graph.nodes.append(cand)
            id_remap[orig_id] = new_id
            titles = ", ".join(f"{o.title} ({o.id})" for o in explicit)
            if proposed:
                cand.status = "proposed"
                cand.supersedes = [o.id for o in explicit]  # intended, applied on confirm
            else:
                for old in explicit:
                    _supersede(graph, cand, old, meeting_id)
            graph.add_edge(new_id, meeting_id, "DECIDED_IN")
            changelog.append(
                f"~ CHANGED {cand.type.value}: {titles} -> {cand.title} ({new_id}) [explicit]{tag}"
            )
            continue

        # (2) similarity-based resolution
        match, is_same = _best_match(cand, graph, emb)

        if not is_same:
            final_id = _unique_id(cand.id, graph)
            cand.id = final_id
            cand.supersedes = []
            if proposed:
                cand.status = "proposed"
            graph.nodes.append(cand)
            id_remap[orig_id] = final_id
            graph.add_edge(final_id, meeting_id, "DECIDED_IN")
            changelog.append(f"+ NEW {cand.type.value}: {cand.title} ({final_id}){tag}")
            continue

        if _is_identical(cand, match, emb):
            id_remap[orig_id] = match.id
            graph.add_edge(match.id, meeting_id, "DECIDED_IN")
            changelog.append(f"= UNCHANGED {match.type.value}: {match.title} ({match.id})")
            continue

        # same entity, changed info ⇒ supersede (deferred if proposed)
        new_id = _unique_id(orig_id if orig_id != match.id else f"{match.id}-v2", graph)
        cand.id = new_id
        cand.supersedes = []
        graph.nodes.append(cand)
        id_remap[orig_id] = new_id
        if proposed:
            cand.status = "proposed"
            cand.supersedes = [match.id]  # applied on confirm
        else:
            _supersede(graph, cand, match, meeting_id)
        graph.add_edge(new_id, meeting_id, "DECIDED_IN")
        changelog.append(
            f"~ CHANGED {cand.type.value}: {match.title} ({match.id}) -> {cand.title} ({new_id}){tag}"
        )

    # remap and add candidate edges using final ids
    for edge in candidate.edges:
        src = id_remap.get(edge.src, edge.src)
        dst = id_remap.get(edge.dst, edge.dst)
        if graph.node_by_id(src) and graph.node_by_id(dst):
            graph.add_edge(src, dst, edge.type)

    if id_remap_out is not None:
        id_remap_out.update(id_remap)
    return graph, changelog
