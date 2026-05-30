"""Anonymous channel-statistics telemetry.

When enabled (default), once per INTERVAL_SECONDS the app POSTs a small
JSON payload to ENDPOINT_URL.

Payload v1.2 contents:
  - Channel-level stats for channels not yet reported (username, member count,
    file count, total size, per-type breakdown). Each channel is sent at most
    once (tracked in telemetry_sent_groups).
  - File-level data: up to 3 000 unsent files per payload (name + size,
    grouped by channel username). Each file is sent at most once (tracked in
    telemetry_sent_files). "fr" field carries how many files remain after
    this batch so the receiver knows more payloads are coming.

Both channel and file dedup tables are updated only after a successful POST.
"""
import asyncio
import gzip
import json as _json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp

import database

_loop_task: Optional[asyncio.Task] = None

ENDPOINT_URL     = "https://www.tankado.com/projects/telfiles/telemetry.php"
INTERVAL_SECONDS = 86400
TELEMETRY_SECRET = os.environ.get("TELEMETRY_SECRET", "")


async def collect_payload() -> tuple[Dict, List[int], List[int]]:
    """Build payload. Returns (payload, group_ids, file_ids)."""
    settings = await database.get_telemetry_settings()
    accounts = await database.list_accounts()

    # ── Channel-level stats (new channels only) ───────────────────────────────
    chan_rows = await database.get_groups_for_telemetry()
    group_ids: List[int] = []
    groups: List[Dict] = []
    for r in chan_rows:
        group_ids.append(int(r["id"]))
        groups.append({
            "u":  r.get("username"),
            "ic": int(bool(r.get("is_channel"))),
            "mc": r.get("member_count"),
            "fc": int(r.get("file_count") or 0),
            "sz": int(r.get("total_size") or 0),
            "ft": _compact_ft(r),
        })

    # ── File-level data (batched, any channel) ────────────────────────────────
    file_rows, has_more = await database.get_files_for_telemetry()
    file_ids: List[int] = []
    # Group files by channel username to reduce key repetition.
    files_by_channel: Dict[str, List[Dict]] = {}
    for r in file_rows:
        file_ids.append(int(r["id"]))
        uname = r.get("username") or ""
        files_by_channel.setdefault(uname, []).append({
            "n":  r.get("file_name"),
            "sz": int(r.get("file_size") or 0),
        })

    payload = {
        "ts":  datetime.utcnow().isoformat() + "Z",
        "iid": settings.get("install_id"),
        "v":   "1.2",
        "na":  len(accounts),
        "g":   groups,
        "f":   files_by_channel,   # {"channame": [{"n":"file.mkv","sz":123}, ...]}
        "fr":  1 if has_more else 0,  # non-zero = more batches remain
    }
    return payload, group_ids, file_ids


def _compact_ft(r: dict) -> dict:
    """Return file-type counts with abbreviated keys, omitting zeros."""
    mapping = {
        "type_audio":    "au",
        "type_video":    "vi",
        "type_image":    "im",
        "type_archive":  "ar",
        "type_document": "do",
        "type_software": "so",
        "type_torrent":  "to",
        "type_other":    "ot",
    }
    return {short: int(r[long]) for long, short in mapping.items()
            if r.get(long)}


async def _send_silently() -> bool:
    payload, group_ids, file_ids = await collect_payload()

    if not group_ids and not file_ids:
        return True  # nothing new to send; not a failure

    headers = {
        "User-Agent":        "TelFiles/1.0 telemetry",
        "X-Telemetry-Secret": TELEMETRY_SECRET,
        "Content-Type":      "application/json; charset=utf-8",
        "Content-Encoding":  "gzip",
    }
    compressed = gzip.compress(_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                               compresslevel=6)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ENDPOINT_URL, data=compressed,
                timeout=aiohttp.ClientTimeout(total=60),
                headers=headers,
            ) as r:
                ok = r.status < 400
    except Exception:
        return False

    if ok:
        try:
            await database.mark_groups_sent(group_ids)
            await database.mark_files_sent(file_ids)
        except Exception:
            pass
    return ok


async def telemetry_loop():
    """Background loop. Silent: no logs, no DB error writes, no UI surface."""
    while True:
        try:
            await asyncio.sleep(300)  # check every 5 min
            settings = await database.get_telemetry_settings()
            if not settings.get("enabled"):
                continue
            now = datetime.now(timezone.utc)
            nxt = settings.get("next_send_at")
            if nxt and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if nxt and nxt > now:
                continue
            ok = await _send_silently()
            try:
                await database.update_telemetry_settings({
                    "next_send_at": now + timedelta(
                        seconds=INTERVAL_SECONDS if ok else 3600
                    ),
                })
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def start_telemetry_loop():
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(telemetry_loop())
    return _loop_task
