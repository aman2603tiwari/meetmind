"""Graph schema — the shape of the context graph a coding agent consumes.

The graph is a JSON document:
    { "meta": {...}, "nodes": [Node, ...], "edges": [Edge, ...] }

Every node carries provenance (which meeting created it) and temporal validity
(`status` + `supersedes`) so the graph records how decisions *evolved*, not just
the current state. A coding agent reads all `active` nodes as current truth and
uses `superseded` nodes + the git diff to know what changed since last meeting.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# --- controlled vocabularies -------------------------------------------------

NODE_TYPES = [
    "Feature",      # a capability the product should have
    "Requirement",  # a concrete thing the code must do
    "Decision",     # a choice made (tech, design, scope)
    "Constraint",   # a limit the code must respect (perf, security, budget)
    "Component",    # a module/service/system referenced
    "Interface",    # an API / endpoint / contract
    "OpenQuestion", # unresolved item the agent should NOT assume
    "ActionItem",   # a task assigned out of the meeting
]

EDGE_TYPES = [
    "IMPLEMENTS",   # Component/Requirement implements a Feature
    "DEPENDS_ON",   # X needs Y first
    "BLOCKS",       # X blocks Y
    "SUPERSEDES",   # new node replaces an old one (temporal)
    "DECIDED_IN",   # node was decided in a meeting
    "RELATES_TO",   # generic association
]

NodeStatus = str  # "active" | "superseded" | "deprecated"


class NodeType(str, Enum):
    Feature = "Feature"
    Requirement = "Requirement"
    Decision = "Decision"
    Constraint = "Constraint"
    Component = "Component"
    Interface = "Interface"
    OpenQuestion = "OpenQuestion"
    ActionItem = "ActionItem"


# --- core models -------------------------------------------------------------


class Node(BaseModel):
    id: str = Field(description="stable unique id, e.g. 'decision-api-style'")
    type: NodeType
    title: str = Field(description="short human-readable label")
    content: str = Field(description="the full statement / what was decided")
    status: NodeStatus = "active"
    # optional at parse time: the LLM omits it and the pipeline stamps it after validation
    source_meeting: str = Field(default="", description="meeting_id that produced this node")
    created_at: str = Field(default="", description="ISO timestamp; set by pipeline")
    supersedes: List[str] = Field(default_factory=list, description="ids this node replaces")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    owner: Optional[str] = Field(default=None, description="person responsible, if stated")
    project: str = Field(default="", description="project this node belongs to (routing)")

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, v):
        """Accept case/spacing variants from the LLM (e.g. 'decision', 'open question')."""
        if isinstance(v, NodeType):
            return v
        if isinstance(v, str):
            key = v.strip().replace("_", "").replace("-", "").replace(" ", "").lower()
            for member in NodeType:
                if member.value.lower() == key:
                    return member
        return v  # let pydantic raise a clear error if still unknown

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.8


class Edge(BaseModel):
    src: str = Field(alias="from", description="source node id")
    dst: str = Field(alias="to", description="target node id")
    type: str

    model_config = {"populate_by_name": True}

    def key(self) -> tuple:
        return (self.src, self.dst, self.type)


class GraphMeta(BaseModel):
    schema_version: str = "0.1.0"
    project: str = Field(default="", description="project/namespace this graph belongs to")
    meetings: List[str] = Field(default_factory=list, description="ingested meeting ids, in order")
    meetily_ingested: List[str] = Field(
        default_factory=list,
        description="Meetily source meeting ids already ingested (for --new/--since dedup)",
    )
    ingested_hashes: List[str] = Field(
        default_factory=list,
        description="sha256 of transcripts already ingested (idempotent re-ingest guard)",
    )


class Graph(BaseModel):
    meta: GraphMeta = Field(default_factory=GraphMeta)
    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)

    # --- convenience helpers ---

    def node_by_id(self, node_id: str) -> Optional[Node]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def active_nodes(self) -> List[Node]:
        return [n for n in self.nodes if n.status == "active"]

    def has_edge(self, src: str, dst: str, type_: str) -> bool:
        return any(e.key() == (src, dst, type_) for e in self.edges)

    def add_edge(self, src: str, dst: str, type_: str) -> None:
        if not self.has_edge(src, dst, type_):
            self.edges.append(Edge(src=src, dst=dst, type=type_))

    @classmethod
    def empty(cls) -> "Graph":
        return cls()


class CandidateGraph(BaseModel):
    """What the LLM extracts from a single transcript — ids are provisional."""

    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)
