"""Sentence-embedding service for semantic file search.

Uses multilingual-e5-small (384-dim, 25 languages including Turkish).
The model is loaded lazily on first use and shared as a single instance.
Use embed_passages() for indexing rows, embed_query() for user queries.

If sentence-transformers is not installed or the model fails to load,
all functions degrade to returning None — callers must handle this and
fall back to the lexical (ILIKE/trgm) search path.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

# Model + dim are fixed. Changing requires re-embedding all rows
# (vector(384) column has a fixed dim).
EMBED_MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-small")
EMBED_DIM = 384

_model = None
_model_lock = asyncio.Lock()
_load_failed = False  # set on first failure to prevent retry loops


async def _ensure_model():
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    async with _model_lock:
        if _model is not None:
            return _model
        if _load_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model: %s", EMBED_MODEL)
            loop = asyncio.get_event_loop()
            _model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(EMBED_MODEL)
            )
            logger.info("Embedding model loaded (dim=%d).", EMBED_DIM)
        except Exception as e:
            logger.warning(
                "Embedding model load failed (%s) — semantic search disabled.", e
            )
            _load_failed = True
            return None
    return _model


def build_composite_text(
    file_name: str,
    group_name: Optional[str],
    context: Optional[str],
) -> str:
    """Combine the three signal sources. file_name is listed first (gets
    more weight in mean-pooling); group_name and context are topical
    context. context is truncated so it doesn't dominate the pool."""
    parts: List[str] = []
    if file_name:
        parts.append(file_name.strip())
    if group_name:
        parts.append(group_name.strip())
    if context:
        c = context.strip()
        if len(c) > 200:
            c = c[:200].rstrip() + "…"
        if c:
            parts.append(c)
    return " | ".join(parts)


async def embed_passages(texts: List[str]) -> Optional[List[List[float]]]:
    """Embed a batch of file passages. Returns None if model unavailable."""
    if not texts:
        return []
    model = await _ensure_model()
    if model is None:
        return None
    # E5 convention: prefix passages and queries differently. The model
    # was trained with this convention; skipping it degrades quality.
    prefixed = [f"passage: {t}" for t in texts]
    loop = asyncio.get_event_loop()
    vecs = await loop.run_in_executor(
        None,
        lambda: model.encode(
            prefixed, normalize_embeddings=True, show_progress_bar=False
        ),
    )
    return [v.tolist() for v in vecs]


# Tiny LRU for query embeddings — repeat searches don't re-encode.
_query_cache: "dict[str, List[float]]" = {}
_QUERY_CACHE_MAX = 256


async def embed_query(text: str) -> Optional[List[float]]:
    """Embed a single user query string. LRU-cached. Returns None if
    model unavailable."""
    if not text or not text.strip():
        return None
    key = text.strip().lower()
    cached = _query_cache.get(key)
    if cached is not None:
        # Touch for LRU recency
        _query_cache.pop(key)
        _query_cache[key] = cached
        return cached
    model = await _ensure_model()
    if model is None:
        return None
    loop = asyncio.get_event_loop()
    vec = await loop.run_in_executor(
        None,
        lambda: model.encode(
            f"query: {key}", normalize_embeddings=True, show_progress_bar=False
        ),
    )
    v = vec.tolist()
    _query_cache[key] = v
    if len(_query_cache) > _QUERY_CACHE_MAX:
        # Evict oldest (Python 3.7+ preserves insertion order)
        _query_cache.pop(next(iter(_query_cache)))
    return v


def is_loaded() -> bool:
    return _model is not None


def is_disabled() -> bool:
    return _load_failed
