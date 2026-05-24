"""Background worker that populates files.name_embedding for new/legacy rows.

Pulls unembedded rows in batches, calls embed.embed_passages(), writes
back via UPDATE. Yields between batches so the asyncio loop stays
responsive during user interaction.

Pattern mirrors torrent_parse worker: idempotent start(), graceful stop(),
exposes progress for the settings UI.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


_BATCH_SIZE = 64
_IDLE_SLEEP = 10.0    # seconds between empty polls (nothing to embed)
_BATCH_SLEEP = 0.1    # short pause between batches so we don't pin CPU
_MODEL_RETRY_SLEEP = 60.0  # if model unavailable, retry after this long

_worker_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None
_progress = {"done": 0, "remaining": -1, "running": False, "available": True}


async def _embed_batch_loop():
    import database
    import embed as _embed
    while not (_stop_event and _stop_event.is_set()):
        try:
            rows = await database._q(
                """SELECT f.id, f.file_name,
                          COALESCE(g.display_name, g.name) AS group_name,
                          f.context
                   FROM files f
                   JOIN groups g ON g.id = f.group_id
                   WHERE f.name_embedding IS NULL
                     AND f.file_name IS NOT NULL
                   ORDER BY f.id DESC
                   LIMIT $1""",
                _BATCH_SIZE,
            )
        except Exception as e:
            logger.warning("embed_worker fetch failed: %s", e)
            await asyncio.sleep(_IDLE_SLEEP)
            continue

        if not rows:
            _progress["running"] = False
            _progress["remaining"] = 0
            await asyncio.sleep(_IDLE_SLEEP)
            continue

        _progress["running"] = True
        texts = [
            _embed.build_composite_text(
                r["file_name"], r["group_name"], r["context"]
            )
            for r in rows
        ]
        vecs = await _embed.embed_passages(texts)
        if vecs is None:
            logger.info(
                "embed_worker: model unavailable — sleeping %ds before retry.",
                _MODEL_RETRY_SLEEP,
            )
            _progress["available"] = False
            await asyncio.sleep(_MODEL_RETRY_SLEEP)
            continue
        _progress["available"] = True

        # Write back. asyncpg + pgvector adapter accepts list[float] for
        # the `vector` type (registered in database._init_conn).
        try:
            async with database._pool.acquire() as conn:
                async with conn.transaction():
                    for r, v in zip(rows, vecs):
                        await conn.execute(
                            "UPDATE files SET name_embedding = $1 WHERE id = $2",
                            v, r["id"],
                        )
        except Exception as e:
            logger.warning("embed_worker UPDATE failed: %s", e)
            await asyncio.sleep(_IDLE_SLEEP)
            continue

        _progress["done"] += len(rows)

        # Periodic remaining count (cheap: indexed scan on NULL predicate)
        if _progress["done"] % (_BATCH_SIZE * 10) == 0:
            try:
                _progress["remaining"] = int(
                    await database._qval(
                        "SELECT COUNT(*) FROM files WHERE name_embedding IS NULL"
                    )
                    or 0
                )
            except Exception:
                pass

        await asyncio.sleep(_BATCH_SLEEP)


def start():
    """Start the background embedder. Idempotent."""
    global _worker_task, _stop_event
    if _worker_task and not _worker_task.done():
        return
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_embed_batch_loop())
    logger.info("embed_worker started.")


def stop():
    if _stop_event:
        _stop_event.set()


def get_progress() -> dict:
    return dict(_progress)
