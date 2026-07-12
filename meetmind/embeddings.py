"""Optional embedding provider for semantic entity resolution.

Tries, in order:
  1. sentence-transformers (all-MiniLM-L6-v2) — local, offline, free
  2. Google Gemini embeddings — free tier, needs GEMINI_API_KEY (network)
  3. None — caller falls back to string matching (no regression)

Kept fully optional so the pipeline works with zero extra installs; embeddings
just improve dedup/supersession quality when available.
"""

from __future__ import annotations

import math
import os
from typing import Callable, List, Optional

Embedder = Callable[[List[str]], List[List[float]]]

_cached: Optional[Embedder] = None
_resolved = False
_label = "none"


def _opted_in() -> bool:
    return os.environ.get("MEETMIND_EMBEDDINGS", os.environ.get("CG_EMBEDDINGS", "0")) not in ("0", "", None)


def _try_sentence_transformers() -> Optional[Embedder]:
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    model = SentenceTransformer("all-MiniLM-L6-v2")

    def embed(texts: List[str]) -> List[List[float]]:
        return [list(map(float, v)) for v in model.encode(texts, normalize_embeddings=False)]

    return embed


def _try_gemini() -> Optional[Embedder]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    try:
        import google.generativeai as genai
    except Exception:
        return None
    genai.configure(api_key=key)

    def embed(texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            r = genai.embed_content(model="models/text-embedding-004", content=t)
            out.append([float(x) for x in r["embedding"]])
        return out

    return embed


def get_embedder(force: str | None = None) -> Optional[Embedder]:
    """Return the best available embedder, or None. Cached after first call.

    force: 'st' | 'gemini' | 'none' to pin a provider (mainly for tests).
    """
    global _cached, _resolved, _label
    if force == "none":
        return None
    if _resolved and force is None:
        return _cached

    if force == "st":
        _cached, _label = _try_sentence_transformers(), "sentence-transformers"
    elif force == "gemini":
        _cached, _label = _try_gemini(), "gemini"
    elif not _opted_in():
        # Semantic matching is opt-in: even when sentence-transformers is
        # installed (e.g. via meetmind[all]), don't load a torch model on every
        # ingest. Enable with MEETMIND_EMBEDDINGS=1. String matching is the default.
        _cached, _label = None, "none (set MEETMIND_EMBEDDINGS=1 for semantic matching)"
    else:
        st = _try_sentence_transformers()
        if st is not None:
            _cached, _label = st, "sentence-transformers"
        else:
            gm = _try_gemini()
            _cached, _label = gm, ("gemini" if gm else "none")

    if _cached is None:
        _label = "none"
    _resolved = True
    return _cached


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def provider_name() -> str:
    get_embedder()
    return _label
