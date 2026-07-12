"""Reusable ingest pipeline shared by the CLI and the Slack bot.

One function turns a transcript into graph changes: extract -> merge -> save ->
git commit. No printing, no argparse — callers handle presentation.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from . import extract as extract_mod
from . import embeddings, store
from .merge import merge
from .schema import CandidateGraph, Graph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def ingest_transcript(
    transcript: str,
    meeting_id: str,
    graph_path: str,
    *,
    commit: bool = True,
    meetily_id: str | None = None,
    api_key: str | None = None,
    project: str | None = None,
) -> Tuple[Graph, List[str]]:
    """Ingest one transcript into the graph at `graph_path`.

    Returns (updated_graph, changelog). When commit is False, nothing is written
    to disk (dry run) — the returned graph reflects the merge in memory.
    """
    graph = store.load_graph(graph_path)
    if project and not graph.meta.project:
        graph.meta.project = project

    # idempotent re-ingest: identical transcript already folded in → skip (saves LLM cost)
    digest = _hash(transcript)
    if digest in graph.meta.ingested_hashes:
        return graph, ["= already ingested (identical transcript) — skipped"]

    threshold = float(os.environ.get("CG_CONFIDENCE_THRESHOLD", "0") or 0)

    # give the extractor the current active nodes so it can emit explicit supersedes
    candidate = extract_mod.extract(
        transcript, meeting_id, api_key=api_key, existing_nodes=graph.active_nodes()
    )

    embedder = embeddings.get_embedder()  # None if no provider installed → string matching
    graph, changelog = merge(
        graph, candidate, meeting_id, _now(), embedder=embedder, confidence_threshold=threshold
    )
    graph.meta.ingested_hashes.append(digest)
    if meetily_id and meetily_id not in graph.meta.meetily_ingested:
        graph.meta.meetily_ingested.append(meetily_id)

    if commit:
        repo_dir = str(Path(graph_path).resolve().parent)
        store.save_graph(graph_path, graph)
        tpath = store.save_transcript(repo_dir, meeting_id, transcript)
        store.git_commit(
            repo_dir, [str(Path(graph_path).resolve()), tpath],
            f"meeting {meeting_id}: {len(changelog)} changes",
        )

    return graph, changelog


def ingest_transcript_routed(
    transcript: str,
    meeting_id: str,
    graph_dir: str,
    *,
    commit: bool = True,
    default_project: str = "general",
    api_key: str | None = None,
) -> Dict[str, Tuple[Graph, List[str], str]]:
    """Route ONE transcript into MULTIPLE per-project graphs (multi-project meeting).

    The extractor labels each node with a project; nodes are grouped and merged
    into context-graphs/<slug>.json independently. Returns
    {slug: (graph, changelog, display_name)}.
    """
    from . import projects

    digest = _hash(transcript)
    threshold = float(os.environ.get("CG_CONFIDENCE_THRESHOLD", "0") or 0)
    known = projects.known_names(graph_dir)
    existing = projects.existing_active_nodes(graph_dir)

    # project of each existing node id (for resolving edges to prior-meeting nodes)
    existing_project = {n.id: projects.slugify(n.project) for n in existing}

    candidate = extract_mod.extract(
        transcript, meeting_id, api_key=api_key,
        existing_nodes=existing, known_projects=known,
    )
    groups = projects.group_candidate_by_project(candidate, default_project)
    embedder = embeddings.get_embedder()

    # map each candidate node's original id -> its project slug
    cand_project = {n.id: slug for slug, nodes in groups.items() for n in nodes}

    repo_dir = str(Path(graph_dir).resolve())
    results: Dict[str, Tuple[Graph, List[str], str]] = {}
    graphs: Dict[str, Graph] = {}
    remaps: Dict[str, dict] = {}
    skipped: set = set()

    for slug, nodes in groups.items():
        gpath = str((Path(graph_dir) / f"{slug}.json").resolve())
        graph = store.load_graph(gpath)
        display = next((n.project for n in nodes if n.project), None) or graph.meta.project or slug

        if digest in graph.meta.ingested_hashes:
            results[slug] = (graph, ["= already ingested — skipped"], display)
            graphs[slug] = graph
            skipped.add(slug)
            continue
        if not graph.meta.project:
            graph.meta.project = display

        remap: dict = {}
        # edges handled in the cross-project pass below → pass none here
        sub = CandidateGraph(nodes=nodes, edges=[])
        graph, changelog = merge(
            graph, sub, meeting_id, _now(),
            embedder=embedder, confidence_threshold=threshold, id_remap_out=remap,
        )
        graph.meta.ingested_hashes.append(digest)
        results[slug] = (graph, changelog, display)
        graphs[slug] = graph
        remaps[slug] = remap

    # --- cross-project / cross-meeting edge resolution ---
    def resolve(node_id: str):
        """Return (slug, final_id) for a candidate or existing node id, else None."""
        if node_id in cand_project:
            slug = cand_project[node_id]
            if slug in skipped:
                return None
            return slug, remaps.get(slug, {}).get(node_id, node_id)
        if node_id in existing_project:
            return existing_project[node_id], node_id
        return None

    for e in candidate.edges:
        rs, rd = resolve(e.src), resolve(e.dst)
        if not rs or not rd:
            continue
        (sslug, sfinal), (dslug, dfinal) = rs, rd
        g = graphs.get(sslug)
        if g is None:
            continue
        dst_ref = dfinal if sslug == dslug else projects.qualify(dslug, dfinal)
        g.add_edge(sfinal, dst_ref, e.type)

    if commit:
        changed = [s for s in results if s not in skipped]
        paths = []
        for slug in changed:
            gpath = str((Path(graph_dir) / f"{slug}.json").resolve())
            store.save_graph(gpath, graphs[slug])
            paths.append(gpath)
        if paths:
            tpath = store.save_transcript(repo_dir, meeting_id, transcript)
            store.git_commit(
                repo_dir, paths + [tpath],
                f"meeting {meeting_id}: {len(paths)} project(s)",
            )

    return results
