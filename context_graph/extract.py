"""Extract a candidate context graph from one transcript, using a Groq LLM.

One chat call with a strict JSON contract. We validate the result against the
schema and retry once on any parse/validation failure. The extraction is
deliberately conservative: capture decisions/requirements/constraints a coding
agent must obey, and DROP chit-chat.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from .schema import CandidateGraph, EDGE_TYPES, NODE_TYPES, Node

GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")

_SYSTEM = """You extract a software-project CONTEXT GRAPH from a meeting transcript.
The consumer is an autonomous CODING AGENT that will write/modify code from this
graph, so capture only durable, actionable engineering information. Ignore
greetings, scheduling, and small talk.

Return ONLY a JSON object of this exact shape (no prose, no markdown fences):
{
  "nodes": [
    {
      "id": "kebab-case-stable-id",
      "type": one of %(node_types)s,
      "title": "short label",
      "content": "the full statement / what was decided or required",
      "confidence": 0.0-1.0,
      "owner": "person name or null",
      "supersedes": ["existing-node-id", ...]   // OPTIONAL, see rules
    }
  ],
  "edges": [ { "from": "node-id", "to": "node-id", "type": one of %(edge_types)s } ]
}

Rules:
- Choose ids that describe the thing (e.g. "decision-api-graphql", "req-user-login"),
  so the SAME concept discussed in a later meeting naturally gets a similar id.
- You are given EXISTING ACTIVE NODES from prior meetings. If this meeting CHANGES,
  REVERSES, or REPLACES one of them, emit the NEW node and set its "supersedes" to
  the exact existing id(s) it replaces. If it merely repeats an existing node
  unchanged, do NOT emit it again. Only emit new or changed information.
- A reversed/changed decision is still a Decision node describing the NEW choice,
  with "supersedes" pointing at the old decision's id.
- Use OpenQuestion for anything explicitly left unresolved — the agent must not assume it.
- Edges may reference existing node ids too (e.g. IMPLEMENTS an existing Feature).
- Do not invent facts not present in the transcript.
- Omit "status"/"source_meeting"; the pipeline sets those. "supersedes" defaults to [].
""" % {"node_types": NODE_TYPES, "edge_types": EDGE_TYPES}


_ROUTING_RULES = """
MULTI-PROJECT ROUTING: this meeting may discuss several projects. Add a "project"
field to EVERY node naming which project it belongs to. Reuse a KNOWN PROJECT name
exactly when it fits; propose a short new name for a genuinely new project; use
"general" for shared/cross-cutting items. Keep a node's project consistent with any
existing node it supersedes.
KNOWN PROJECTS: %s
"""


def _existing_nodes_block(existing: Optional[List[Node]], routed: bool = False) -> str:
    if not existing:
        return "EXISTING ACTIVE NODES: (none — this is the first meeting)"
    compact = []
    for n in existing:
        item = {"id": n.id, "type": n.type.value, "title": n.title, "content": n.content[:160]}
        if routed and n.project:
            item["project"] = n.project
        compact.append(item)
    return "EXISTING ACTIVE NODES (you may supersede or reference these by id):\n" + json.dumps(
        compact, ensure_ascii=False, indent=0
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _call_groq(transcript: str, api_key: str, force_json: bool, system: str, existing_block: str) -> str:
    from groq import Groq

    client = Groq(api_key=api_key)
    user = f"{existing_block}\n\nTranscript:\n\n{transcript}"
    kwargs = {
        "model": GROQ_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
    }
    if force_json:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or "{}"


def extract(
    transcript: str,
    meeting_id: str,
    api_key: str | None = None,
    existing_nodes: Optional[List[Node]] = None,
    known_projects: Optional[List[str]] = None,
) -> CandidateGraph:
    """Extract a candidate graph. If known_projects is not None, ROUTED mode is on:
    every node gets a `project` label (multi-project meetings)."""
    from ._apikey import resolve_groq_key
    api_key = resolve_groq_key(api_key)
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — needed for extraction")

    routed = known_projects is not None
    system = _SYSTEM
    if routed:
        names = ", ".join(known_projects) if known_projects else "(none yet)"
        system = _SYSTEM + (_ROUTING_RULES % names)

    existing_block = _existing_nodes_block(existing_nodes, routed=routed)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            raw = _call_groq(transcript, api_key, True, system, existing_block)
            data = json.loads(_strip_fences(raw))
            candidate = CandidateGraph.model_validate(data)
            for node in candidate.nodes:
                node.source_meeting = meeting_id
                node.status = "active"
            return candidate
        except Exception as err:  # parse or validation failure → retry once
            last_err = err
    raise ValueError(f"extraction failed after retries: {last_err}")
