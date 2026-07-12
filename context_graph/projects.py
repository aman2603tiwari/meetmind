"""Project discovery + routing helpers for multi-project meetings.

A single meeting can touch several projects. We keep one graph file per project
under a directory (default: context-graphs/<slug>.json). This module discovers
those projects, gathers their active nodes (so the extractor can supersede/label
consistently), and slugifies project names.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from . import store
from .schema import Graph, Node

GENERAL = "general"  # bucket for shared / cross-cutting nodes
QUALIFIER = ":"       # cross-project reference: "<project-slug>:<node-id>"


def qualify(slug: str, node_id: str) -> str:
    return f"{slug}{QUALIFIER}{node_id}"


def is_qualified(node_id: str) -> bool:
    return QUALIFIER in node_id


def split_qualified(node_id: str) -> Tuple[str, str]:
    slug, _, nid = node_id.partition(QUALIFIER)
    return slug, nid


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or GENERAL


def graph_path(graph_dir: str, slug: str) -> str:
    return str(Path(graph_dir) / f"{slugify(slug)}.json")


def discover(graph_dir: str) -> List[Tuple[str, str, Graph]]:
    """Return [(slug, display_name, graph)] for every project graph on disk."""
    d = Path(graph_dir)
    out: List[Tuple[str, str, Graph]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        graph = store.load_graph(str(f))
        slug = f.stem
        display = graph.meta.project or slug
        out.append((slug, display, graph))
    return out


def known_names(graph_dir: str) -> List[str]:
    """Human-readable project names already on disk (for the extractor prompt)."""
    return [display for _, display, _ in discover(graph_dir)]


def existing_active_nodes(graph_dir: str) -> List[Node]:
    """All active nodes across projects, each stamped with its project display name.

    Lets the extractor keep project labels consistent and supersede the right node.
    """
    nodes: List[Node] = []
    for _slug, display, graph in discover(graph_dir):
        for n in graph.active_nodes():
            n.project = display
            nodes.append(n)
    return nodes


def group_candidate_by_project(candidate, default_project: str) -> Dict[str, list]:
    """Split candidate nodes into {slug: [nodes]} by their assigned project.

    Nodes with no project fall back to default_project.
    """
    groups: Dict[str, list] = {}
    for n in candidate.nodes:
        slug = slugify(n.project or default_project)
        groups.setdefault(slug, []).append(n)
    return groups
