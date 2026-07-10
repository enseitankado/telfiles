"""Partial torrent download for inspecting archives without downloading entire files.

Two-pass strategy:
  1. Fetch .torrent metadata via aria2c (DHT) into a temp dir.
  2. Run a second aria2c using the saved .torrent file with --select-file for
     archive entries only, plus --bt-prioritize-piece=head=1M,tail=4M.
     This ensures the archive headers/footers are downloaded first.
  3. Poll the partial files and attempt format-specific parsing:
       ZIP  — manual EOCD + Central Directory parser (tail bytes)
       RAR  — rarfile reads headers from file beginning (head bytes)
       7z   — 7z CLI 'l' command (needs both head SignatureHeader + tail packed header)
  4. Kill aria2c when all archives are parsed or timeout expires.

File size limit: archives > 500 MB are skipped to avoid thrashing disk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ARIA_META_ARGS = [
    "--bt-metadata-only=true",
    "--bt-save-metadata=true",
    "--summary-interval=0",
    "--console-log-level=error",
    "--check-certificate=false",
    "--bt-stop-timeout=0",
    "--enable-dht=true",
    "--enable-peer-exchange=true",
    "--bt-tracker-connect-timeout=8",
    "--bt-tracker-timeout=8",
    "--allow-overwrite=true",
    "--seed-time=0",
    "--max-concurrent-downloads=1",
]

_ARIA_DL_ARGS = [
    "--summary-interval=0",
    "--console-log-level=error",
    "--check-certificate=false",
    "--bt-stop-timeout=0",
    "--enable-dht=true",
    "--enable-peer-exchange=true",
    "--allow-overwrite=true",
    "--seed-time=0",
    "--file-allocation=none",
]

_ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
_MAX_ARCHIVE_BYTES = 500 * 1024 * 1024   # skip archives > 500 MB


async def _fetch_torrent_file(magnet_uri: str, dest_dir: str, timeout: int = 90) -> Optional[str]:
    """Fetch .torrent file from DHT into dest_dir. Returns file path or None."""
    cmd = ["aria2c", *_ARIA_META_ARGS, "-d", dest_dir, magnet_uri]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return None
    except FileNotFoundError:
        logger.error("archive_inspector: aria2c not found in PATH")
        return None
    except Exception as exc:
        logger.warning("archive_inspector: metadata fetch error: %s", exc)
        return None

    return next(
        (os.path.join(dest_dir, fn) for fn in os.listdir(dest_dir) if fn.endswith(".torrent")),
        None,
    )


# ── ZIP parser ─────────────────────────────────────────────────────────────────

def _parse_zip_tail(tail: bytes, total_size: int) -> Optional[List[Dict]]:
    """Extract file list from ZIP Central Directory contained in tail bytes.

    tail = the last N bytes of the file (as downloaded from the tail pieces).
    total_size = actual declared size of the archive.
    """
    EOCD_SIG = b"PK\x05\x06"

    # Search for EOCD from the end.  ZIP comment can be up to 65535 bytes.
    eocd_pos = -1
    for i in range(len(tail) - 22, max(-1, len(tail) - 65558), -1):
        if tail[i : i + 4] == EOCD_SIG:
            eocd_pos = i
            break
    if eocd_pos < 0 or len(tail) - eocd_pos < 22:
        return None

    cd_size   = struct.unpack_from("<I", tail, eocd_pos + 12)[0]
    cd_offset = struct.unpack_from("<I", tail, eocd_pos + 16)[0]

    # Handle ZIP64 (sizes are 0xFFFFFFFF → look for ZIP64 EOCD locator)
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        LOC_SIG  = b"PK\x06\x07"
        loc_pos  = tail.rfind(LOC_SIG, 0, eocd_pos)
        Z64_SIG  = b"PK\x06\x06"
        if loc_pos >= 0 and len(tail) - loc_pos >= 20:
            z64_abs      = struct.unpack_from("<Q", tail, loc_pos + 8)[0]
            tail_start   = total_size - len(tail)
            z64_in_tail  = z64_abs - tail_start
            if 0 <= z64_in_tail < len(tail) and tail[z64_in_tail : z64_in_tail + 4] == Z64_SIG:
                if len(tail) - z64_in_tail >= 56:
                    cd_size   = struct.unpack_from("<Q", tail, z64_in_tail + 40)[0]
                    cd_offset = struct.unpack_from("<Q", tail, z64_in_tail + 48)[0]

    tail_start  = total_size - len(tail)
    cd_in_tail  = cd_offset - tail_start

    if cd_in_tail < 0 or cd_in_tail + cd_size > len(tail):
        return None   # Central Directory not yet in our tail window

    CD_ENTRY = b"PK\x01\x02"
    files: List[Dict] = []
    pos = cd_in_tail
    end = cd_in_tail + cd_size

    while pos < end and pos + 4 <= len(tail):
        if tail[pos : pos + 4] != CD_ENTRY:
            break
        if pos + 46 > len(tail):
            break

        uncomp   = struct.unpack_from("<I", tail, pos + 24)[0]
        fn_len   = struct.unpack_from("<H", tail, pos + 28)[0]
        ex_len   = struct.unpack_from("<H", tail, pos + 30)[0]
        cm_len   = struct.unpack_from("<H", tail, pos + 32)[0]
        fn_end   = pos + 46 + fn_len
        if fn_end > len(tail):
            break

        try:
            fname = tail[pos + 46 : fn_end].decode("utf-8")
        except UnicodeDecodeError:
            fname = tail[pos + 46 : fn_end].decode("latin-1", errors="replace")

        size = uncomp
        if size == 0xFFFFFFFF:
            extra = tail[fn_end : fn_end + ex_len]
            ep = 0
            while ep + 4 <= len(extra):
                hid = struct.unpack_from("<H", extra, ep)[0]
                hsz = struct.unpack_from("<H", extra, ep + 2)[0]
                if hid == 0x0001 and hsz >= 8:
                    size = struct.unpack_from("<Q", extra, ep + 4)[0]
                    break
                ep += 4 + hsz

        if fname and not fname.endswith("/"):
            files.append({"name": fname, "size": size})

        pos += 46 + fn_len + ex_len + cm_len

    return files if files else None


# ── RAR parser ─────────────────────────────────────────────────────────────────

def _list_rar(partial_path: str) -> Optional[List[Dict]]:
    try:
        import rarfile
        rarfile.NEED_COMMENTS = 0
        rf = rarfile.RarFile(partial_path, errors="ignore")
        files = [
            {"name": info.filename, "size": info.file_size}
            for info in rf.infolist()
            if not info.is_dir()
        ]
        return files or None
    except Exception as exc:
        logger.debug("RAR parse failed (%s): %s", partial_path, exc)
        return None


# ── 7z parser ──────────────────────────────────────────────────────────────────

async def _list_7z(partial_path: str) -> Optional[List[Dict]]:
    """Use the 7z CLI to list archive contents.

    The `7z l -slt` output has one archive-info block (identified by a `Type =`
    field) followed by per-file blocks separated by blank lines.  The first
    `----------` marks the start of file blocks; subsequent blocks end on a
    blank line.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "7z", "l", "-slt", partial_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            return None

        output = stdout.decode("utf-8", errors="replace")
        files: List[Dict] = []
        in_entries = False   # flip on first "----------"
        cur: Dict = {}

        def _flush(c: Dict):
            path = c.get("path")
            if not path or c.get("type"):    # skip archive-info block (has Type=)
                return
            attr = c.get("attr", "")
            parts = attr.split()
            if parts and parts[0] == "D":   # directory
                return
            try:
                sz = int(c.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0
            files.append({"name": path, "size": sz})

        for raw in output.splitlines():
            line = raw.strip()
            if line == "----------":
                _flush(cur)
                cur = {}
                in_entries = True
                continue
            if not in_entries:
                continue
            if not line:
                _flush(cur)
                cur = {}
                continue
            if " = " in line:
                key, _, val = line.partition(" = ")
                k = key.strip().lower()
                if k == "path":
                    cur["path"] = val.strip()
                elif k == "size":
                    cur["size"] = val.strip()
                elif k == "attributes":
                    cur["attr"] = val.strip()
                elif k == "type":
                    cur["type"] = val.strip()

        _flush(cur)
        return files or None
    except FileNotFoundError:
        logger.warning("archive_inspector: 7z binary not found")
        return None
    except Exception as exc:
        logger.debug("7z list failed: %s", exc)
        return None


# ── Per-format try-parse ────────────────────────────────────────────────────────

async def _try_parse(partial_path: str, ext: str, declared_size: int) -> Optional[List[Dict]]:
    try:
        disk_size = os.path.getsize(partial_path)
    except OSError:
        return None

    if ext == ".zip":
        tail_size = min(declared_size, 4 * 1024 * 1024)
        try:
            with open(partial_path, "rb") as f:
                f.seek(declared_size - tail_size)
                tail = f.read(tail_size)
            if len(tail) < 22:
                return None
            return _parse_zip_tail(tail, declared_size)
        except Exception as exc:
            logger.debug("ZIP tail read failed: %s", exc)
            return None

    elif ext == ".rar":
        # RAR headers are at the beginning — head pieces suffice
        return _list_rar(partial_path)

    elif ext == ".7z":
        return await _list_7z(partial_path)

    return None


# ── File locator ────────────────────────────────────────────────────────────────

def _find_file(dl_dir: str, archive_path: str) -> Optional[str]:
    """Locate the partially downloaded archive in the aria2c download dir."""
    direct = os.path.join(dl_dir, archive_path.replace("\\", "/"))
    if os.path.exists(direct):
        return direct

    parts = archive_path.replace("\\", "/").split("/")
    basename = parts[-1]

    # Walk the directory looking for the file by name
    for root, _dirs, files in os.walk(dl_dir):
        if basename in files:
            return os.path.join(root, basename)

    return None


# ── Public API ──────────────────────────────────────────────────────────────────

async def inspect_magnet_archives(
    magnet_uri: str,
    timeout: int = 120,
) -> Dict[str, List[Dict]]:
    """Partially download archives from a magnet torrent and list their contents.

    Returns {archive_path: [{"name": "...", "size": N}]} for each parseable archive.
    Returns {} on failure or if the torrent contains no supported archives.
    """
    if not magnet_uri.lower().startswith("magnet:"):
        return {}

    results: Dict[str, List[Dict]] = {}

    with tempfile.TemporaryDirectory(prefix="archinsp_") as td:
        meta_dir = os.path.join(td, "meta")
        dl_dir   = os.path.join(td, "dl")
        os.makedirs(meta_dir, exist_ok=True)
        os.makedirs(dl_dir, exist_ok=True)

        # Pass 1: fetch .torrent metadata
        meta_timeout = min(90, max(30, timeout - 40))
        logger.info("archive_inspector: fetching metadata (%ss) for %s", meta_timeout, magnet_uri[:80])
        torrent_path = await _fetch_torrent_file(magnet_uri, meta_dir, timeout=meta_timeout)
        if not torrent_path:
            logger.info("archive_inspector: metadata fetch failed/timed out")
            return {}

        # Parse torrent to enumerate files
        import torrent_parse
        try:
            with open(torrent_path, "rb") as f:
                meta = torrent_parse.parse_torrent(f.read())
        except Exception as exc:
            logger.warning("archive_inspector: torrent parse error: %s", exc)
            return {}

        tree = meta.get("tree") or []

        # Find inspectable archives (by extension + size cap)
        targets: List[Tuple[int, str, int, str]] = []   # (1-based-idx, path, size, ext)
        for i, entry in enumerate(tree):
            path = entry.get("path") or ""
            size = int(entry.get("size") or 0)
            ext  = os.path.splitext(path.lower())[1]
            if ext in _ARCHIVE_EXTS and 0 < size <= _MAX_ARCHIVE_BYTES:
                targets.append((i + 1, path, size, ext))

        if not targets:
            logger.info("archive_inspector: no inspectable archives in torrent")
            return {}

        logger.info(
            "archive_inspector: %d target archive(s): %s",
            len(targets),
            [t[1] for t in targets],
        )

        # Pass 2: partial download with head+tail prioritisation
        select = ",".join(str(idx) for idx, _, _, _ in targets)
        cmd = [
            "aria2c",
            *_ARIA_DL_ARGS,
            f"--select-file={select}",
            "--bt-prioritize-piece=head=1M,tail=4M",
            "-d", dl_dir,
            torrent_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Poll until parsed or deadline
        remaining_dl = max(20, timeout - meta_timeout - 5)
        deadline = asyncio.get_event_loop().time() + remaining_dl
        pending = list(targets)

        try:
            while asyncio.get_event_loop().time() < deadline and pending:
                await asyncio.sleep(6)
                still_pending = []
                for idx, path, size, ext in pending:
                    partial = _find_file(dl_dir, path)
                    if not partial:
                        still_pending.append((idx, path, size, ext))
                        continue
                    parsed = await _try_parse(partial, ext, size)
                    if parsed is not None:
                        results[path] = parsed
                        logger.info("archive_inspector: %s → %d files", path, len(parsed))
                    else:
                        still_pending.append((idx, path, size, ext))
                pending = still_pending
        finally:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    return results
