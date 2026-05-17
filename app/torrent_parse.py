"""Torrent file bencode parser and multi-account bulk parse worker.

Workflow:
  1. TorrentParseWorker.start(concurrency) fetches all unparsed .torrent
     files from the DB and downloads+parses them concurrently using up to
     `concurrency` parallel Telegram slots, round-robined across active
     accounts.
  2. parse_single(file_id) handles on-demand parsing for one file — called
     from GET /api/files/{id}/torrent-tree.
  3. Parsing is pure bencode — no tracker connection needed. The file tree
     is embedded in the torrent's `info` dictionary.
  4. Temp files are written to _TMP_DIR and deleted after parsing.
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TMP_DIR = os.environ.get("TORRENT_TMP_DIR", "/tmp/torrent_parse")


# ── Bencode decoder ────────────────────────────────────────────────────────────

def _bdecode(data: bytes, pos: int):
    """Return (value, next_pos). Raises ValueError on malformed data."""
    ch = data[pos : pos + 1]
    if ch == b"i":
        end = data.index(b"e", pos + 1)
        return int(data[pos + 1 : end]), end + 1
    if ch == b"l":
        pos += 1
        result: List[Any] = []
        while data[pos : pos + 1] != b"e":
            item, pos = _bdecode(data, pos)
            result.append(item)
        return result, pos + 1
    if ch == b"d":
        pos += 1
        result: Dict[str, Any] = {}
        while data[pos : pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            val, pos = _bdecode(data, pos)
            if isinstance(key, bytes):
                try:
                    key = key.decode("utf-8")
                except Exception:
                    key = key.decode("latin-1")
            result[key] = val
        return result, pos + 1
    # Byte string: "N:..."
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start : start + length], start + length


def _to_str(v: Any) -> str:
    if isinstance(v, bytes):
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                return v.decode(enc)
            except Exception:
                continue
        return v.decode("latin-1", errors="replace")
    return str(v) if v is not None else ""


def parse_torrent(data: bytes) -> Dict[str, Any]:
    """Parse raw .torrent bytes.

    Returns a dict with keys:
      name        — torrent display name
      total_size  — sum of all file sizes (bytes)
      file_count  — number of files
      tree        — list of {path: str, size: int}
    """
    meta, _ = _bdecode(data, 0)
    info: Dict[str, Any] = meta.get("info") or {}

    name = _to_str(info.get("name.utf-8") or info.get("name") or b"")
    files: List[Dict[str, Any]] = []

    if "files" in info:
        for f in info["files"]:
            path_parts = f.get("path.utf-8") or f.get("path") or []
            rel = "/".join(_to_str(p) for p in path_parts)
            full = f"{name}/{rel}" if name else rel
            files.append({"path": full, "size": int(f.get("length") or 0)})
    else:
        files.append({"path": name, "size": int(info.get("length") or 0)})

    total = sum(f["size"] for f in files)
    return {
        "name": name,
        "total_size": total,
        "file_count": len(files),
        "tree": files,
    }


# ── On-demand single-file parse ────────────────────────────────────────────────

async def parse_single(file_id: int) -> Dict[str, Any]:
    """Download + parse one torrent file. Returns the tree dict.

    Returns cached result immediately if the file was already parsed.
    Raises ValueError for non-torrent files or missing DB rows.
    """
    import database
    import telegram_client as tc

    existing = await database.get_torrent_tree(file_id)
    if existing and existing.get("parsed_at") and not existing.get("error"):
        return existing

    file_info = await database.get_file_by_id(file_id)
    if not file_info:
        raise ValueError(f"File {file_id} not found")
    if (file_info.get("file_ext") or "").lower() != "torrent":
        raise ValueError(f"File {file_id} is not a .torrent file")

    os.makedirs(_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(_TMP_DIR, f"{file_id}.torrent")

    acc_id = file_info.get("discovered_by_account_id") or 1
    try:
        client = await tc.get_client(acc_id)
    except Exception:
        client = await tc.get_client(1)
    if not client.is_connected():
        await client.connect()

    try:
        msg = await client.get_messages(
            file_info["group_id"], ids=int(file_info["message_id"])
        )
        if not msg or not msg.document:
            raise ValueError("Message or document not found on Telegram")

        await client.download_media(msg, tmp_path)

        with open(tmp_path, "rb") as fh:
            result = parse_torrent(fh.read())

        await database.save_torrent_tree(
            file_id,
            result["name"],
            result["total_size"],
            result["file_count"],
            result["tree"],
        )
        logger.info(
            "torrent parsed on-demand: file_id=%d name=%r files=%d",
            file_id,
            result["name"],
            result["file_count"],
        )
        return result
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


# ── Bulk parse worker ──────────────────────────────────────────────────────────

class TorrentParseWorker:
    """Async worker that bulk-downloads and parses all unprocessed .torrent files.

    Each asyncio slot is assigned to one Telegram account via round-robin,
    spreading download load across all active accounts.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self.status: Dict[str, Any] = {
            "running": False,
            "total": 0,
            "done": 0,
            "errors": 0,
        }

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_status(self) -> Dict[str, Any]:
        return dict(self.status)

    def start(self, concurrency: int = 5) -> bool:
        """Start the bulk worker. Returns False if already running."""
        if self.is_running:
            return False
        concurrency = max(1, min(20, concurrency))
        self.status = {"running": True, "total": 0, "done": 0, "errors": 0}
        self._task = asyncio.create_task(self._run(concurrency))
        return True

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self, concurrency: int) -> None:
        import database

        os.makedirs(_TMP_DIR, exist_ok=True)
        try:
            files = await database.get_unparsed_torrents()
            self.status["total"] = len(files)
            if not files:
                return

            accounts = await database.list_accounts()
            acc_ids = [a["id"] for a in accounts if a.get("is_active")]
            if not acc_ids:
                acc_ids = [1]

            sem = asyncio.Semaphore(concurrency)
            await asyncio.gather(
                *(
                    self._parse_one(f, acc_ids[i % len(acc_ids)], sem)
                    for i, f in enumerate(files)
                ),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            self.status["running"] = False

    async def _parse_one(
        self,
        file_info: Dict[str, Any],
        acc_id: int,
        sem: asyncio.Semaphore,
    ) -> None:
        import database
        import telegram_client as tc

        async with sem:
            file_id = file_info["id"]
            tmp_path = os.path.join(_TMP_DIR, f"{file_id}.torrent")
            try:
                try:
                    client = await tc.get_client(acc_id)
                except Exception:
                    client = await tc.get_client(1)
                if not client.is_connected():
                    await client.connect()

                msg = await client.get_messages(
                    file_info["group_id"], ids=int(file_info["message_id"])
                )
                if not msg or not msg.document:
                    await database.save_torrent_tree(
                        file_id, None, 0, 0, [], error="No document found"
                    )
                    self.status["errors"] += 1
                    return

                await client.download_media(msg, tmp_path)

                with open(tmp_path, "rb") as fh:
                    result = parse_torrent(fh.read())

                await database.save_torrent_tree(
                    file_id,
                    result["name"],
                    result["total_size"],
                    result["file_count"],
                    result["tree"],
                )
                self.status["done"] += 1
                logger.info(
                    "torrent bulk-parsed: file_id=%d name=%r files=%d",
                    file_id,
                    result["name"],
                    result["file_count"],
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("torrent parse failed file_id=%d: %s", file_id, exc)
                self.status["errors"] += 1
                try:
                    await database.save_torrent_tree(
                        file_id, None, 0, 0, [], error=str(exc)
                    )
                except Exception:
                    pass
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
