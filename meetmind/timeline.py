"""Meeting-by-meeting evolution view — the temporal story of the graph.

Shows, per meeting (in order), what was introduced and what it replaced, plus
which nodes were later superseded. This is the differentiator: a coding agent (or
human) can read how decisions changed across meetings, not just the end state.
"""

from __future__ import annotations

from typing import Dict, List

from .schema import Graph, Node


def _superseded_by(graph: Graph) -> Dict[str, str]:
    """old_id -> new_id, from SUPERSEDES edges."""
    out = {}
    for e in graph.edges:
        if e.type == "SUPERSEDES":
            out[e.dst] = e.src
    return out


def to_markdown(graph: Graph) -> str:
    order = graph.meta.meetings or sorted({n.source_meeting for n in graph.nodes})
    replaced_by = _superseded_by(graph)
    by_meeting: Dict[str, List[Node]] = {m: [] for m in order}
    for n in graph.nodes:
        by_meeting.setdefault(n.source_meeting, []).append(n)

    lines = [f"# {graph.meta.project or 'Context'} — timeline", ""]
    for m in order:
        nodes = by_meeting.get(m, [])
        lines.append(f"## {m}")
        if not nodes:
            lines.append("_(no nodes)_\n")
            continue
        for n in nodes:
            if n.supersedes:
                repl = ", ".join(n.supersedes)
                lines.append(f"- ~ **{n.type.value}: {n.title}** — replaced `{repl}`")
            else:
                lines.append(f"- + **{n.type.value}: {n.title}**")
            if n.status == "superseded":
                nxt = replaced_by.get(n.id, "?")
                lines.append(f"    - _(later superseded by `{nxt}`)_")
            elif n.status == "proposed":
                lines.append("    - _(proposed — awaiting review)_")
        lines.append("")
    return "\n".join(lines)


def to_mermaid(graph: Graph) -> str:
    """A left-to-right timeline: meeting columns as subgraphs, supersedes as arrows."""
    order = graph.meta.meetings or sorted({n.source_meeting for n in graph.nodes})

    def nid(x):
        return "n_" + "".join(c if c.isalnum() else "_" for c in x)

    lines = ["graph LR"]
    for m in order:
        lines.append(f"  subgraph {nid(m)}[\"{m}\"]")
        for n in graph.nodes:
            if n.source_meeting != m:
                continue
            label = f"{n.type.value}: {n.title}".replace('"', "'")
            lines.append(f'    {nid(n.id)}["{label}"]')
        lines.append("  end")
    for e in graph.edges:
        if e.type == "SUPERSEDES":
            lines.append(f"  {nid(e.dst)} -. replaced by .-> {nid(e.src)}")
    return "\n".join(lines)
