"""Render the context graph to a picture: Graphviz (primary) or Mermaid.

Visual encoding (both renderers):
  - node COLOR   = status:  active=green, superseded=grey, deprecated=red-grey,
                            OpenQuestion(active)=amber
  - node SHAPE   = type:    Decision=diamond, Requirement=box, Constraint=hexagon,
                            Component=component, Interface=parallelogram,
                            OpenQuestion=box(rounded), others=ellipse
  - edge STYLE   = relation: SUPERSEDES=red dashed, others=solid grey
Meeting/provenance nodes and DECIDED_IN edges are omitted to keep it readable.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from .schema import Graph

# --- styling maps ------------------------------------------------------------

_DOT_SHAPE = {
    "Decision": "diamond",
    "Requirement": "box",
    "Constraint": "hexagon",
    "Component": "component",
    "Interface": "parallelogram",
    "OpenQuestion": "box",
    "Feature": "folder",
    "ActionItem": "note",
}

_HIDE_EDGE_TYPES = {"DECIDED_IN"}  # provenance edges to a meeting id, not shown


def _fill(node) -> str:
    if node.status == "superseded":
        return "#d9d9d9"
    if node.status == "deprecated":
        return "#e6b8b8"
    if node.status == "proposed":
        return "#a9d6f5"  # light blue = awaiting review
    if node.type.value == "OpenQuestion":
        return "#ffe08a"
    return "#b7e4c7"  # active


def _wrap(text: str, width: int = 26) -> str:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return "\n".join(out)


def _visible_nodes(graph: Graph, include_superseded: bool):
    for n in graph.nodes:
        # active + proposed always shown; superseded/deprecated only when asked
        if n.status in ("superseded", "deprecated") and not include_superseded:
            continue
        yield n


# --- Graphviz DOT ------------------------------------------------------------


def to_dot(graph: Graph, include_superseded: bool = True) -> str:
    ids = set()
    lines = [
        "digraph context {",
        '  rankdir=LR;',
        '  node [style="filled,rounded", fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=8, color="#888888"];',
    ]
    for n in _visible_nodes(graph, include_superseded):
        ids.add(n.id)
        shape = _DOT_SHAPE.get(n.type.value, "ellipse")
        label = _wrap(f"{n.type.value}: {n.title}")
        lines.append(
            f'  "{n.id}" [label="{label}", shape={shape}, fillcolor="{_fill(n)}"];'
        )
    for e in graph.edges:
        if e.type in _HIDE_EDGE_TYPES:
            continue
        if e.src not in ids or e.dst not in ids:
            continue
        if e.type == "SUPERSEDES":
            style = ' [label="supersedes", color="#c0392b", style=dashed]'
        else:
            style = f' [label="{e.type.lower()}"]'
        lines.append(f'  "{e.src}" -> "{e.dst}"{style};')
    lines.append("}")
    return "\n".join(lines)


# --- Mermaid -----------------------------------------------------------------

_MERMAID_OPEN = {  # (open, close) bracket per node type -> shape
    "Decision": ("{{", "}}"),      # hexagon-ish
    "Requirement": ("[", "]"),
    "Constraint": ("[/", "/]"),
    "Component": ("[(", ")]"),
    "Interface": ("[/", "/]"),
    "OpenQuestion": ("(", ")"),
}


def _mid(node_id: str) -> str:
    return "n_" + "".join(c if c.isalnum() else "_" for c in node_id)


def to_mermaid(graph: Graph, include_superseded: bool = True) -> str:
    lines = ["graph LR"]
    ids = set()
    for n in _visible_nodes(graph, include_superseded):
        ids.add(n.id)
        o, c = _MERMAID_OPEN.get(n.type.value, ("[", "]"))
        text = f"{n.type.value}: {n.title}".replace('"', "'")
        lines.append(f'  {_mid(n.id)}{o}"{text}"{c}')
    for n in _visible_nodes(graph, include_superseded):
        cls = (
            "superseded" if n.status == "superseded"
            else "openq" if n.type.value == "OpenQuestion"
            else "active"
        )
        lines.append(f"  class {_mid(n.id)} {cls}")
    for e in graph.edges:
        if e.type in _HIDE_EDGE_TYPES or e.src not in ids or e.dst not in ids:
            continue
        if e.type == "SUPERSEDES":
            lines.append(f"  {_mid(e.src)} -. supersedes .-> {_mid(e.dst)}")
        else:
            lines.append(f"  {_mid(e.src)} -->|{e.type.lower()}| {_mid(e.dst)}")
    lines += [
        "  classDef active fill:#b7e4c7,stroke:#2d6a4f;",
        "  classDef superseded fill:#d9d9d9,stroke:#888,color:#555;",
        "  classDef openq fill:#ffe08a,stroke:#b8860b;",
    ]
    return "\n".join(lines)


# --- rendering to PNG --------------------------------------------------------


def render_png(graph: Graph, out_path: str, include_superseded: bool = True) -> str:
    """Render to a PNG. Tries local Graphviz first, then the mermaid.ink web
    service as a no-binary fallback. Returns the path written.

    NOTE: the mermaid.ink fallback sends the (diagram) graph to an external
    service — avoid it for confidential meetings; install Graphviz to stay local.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) local Graphviz (fully offline, preferred)
    try:
        import graphviz  # python wrapper; needs the `dot` binary installed

        src = graphviz.Source(to_dot(graph, include_superseded))
        # render writes <out without ext>.png
        stem = str(out.with_suffix(""))
        produced = src.render(filename=stem, format="png", cleanup=True)
        return produced
    except Exception:
        pass

    # 2) mermaid.ink fallback (needs network, sends data out)
    return _render_mermaid_ink(to_mermaid(graph, include_superseded), str(out))


def _render_mermaid_ink(mermaid_text: str, out_path: str) -> str:
    import requests

    encoded = base64.urlsafe_b64encode(mermaid_text.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}?type=png"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    Path(out_path).write_bytes(resp.content)
    return out_path


def available_renderer() -> str:
    """Report which PNG renderer is usable: 'graphviz', 'mermaid.ink', or 'none'."""
    try:
        import graphviz  # noqa: F401
        from shutil import which

        if which("dot"):
            return "graphviz"
    except Exception:
        pass
    try:
        import requests  # noqa: F401

        return "mermaid.ink"
    except Exception:
        return "none"
