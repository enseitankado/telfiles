"""Anonymous channel-statistics telemetry.

When enabled (default), once per INTERVAL_SECONDS the app POSTs a small
JSON payload to ENDPOINT_URL. Endpoint, interval and shared secret are all
hard-coded (env-override for the secret only). No user-visible logging.
"""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import aiohttp

import database

_loop_task: Optional[asyncio.Task] = None

ENDPOINT_URL     = "https://www.tankado.com/projects/telfiles/telemetry.php"
INTERVAL_SECONDS = 86400
TELEMETRY_SECRET = os.environ.get("TELEMETRY_SECRET", "")


async def collect_payload() -> Dict:
    settings = await database.get_telemetry_settings()
    accounts = await database.list_accounts()
    rows = await database.get_groups_for_telemetry()
    groups = [{
        "username": r.get("username"),
        "is_channel": bool(r.get("is_channel")),
        "member_count": r.get("member_count"),
        "file_count": int(r.get("file_count") or 0),
        "total_size": int(r.get("total_size") or 0),
    } for r in rows]
    return {
        "install_id":    settings.get("install_id"),
        "timestamp":     datetime.utcnow().isoformat() + "Z",
        "version":       "1.0",
        "account_count": len(accounts),
        "group_count":   len(groups),
        "groups":        groups,
    }


async def _send_silently() -> bool:
    payload = await collect_payload()
    headers = {"User-Agent": "TelFiles/1.0 telemetry",
               "X-Telemetry-Secret": TELEMETRY_SECRET}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ENDPOINT_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers,
            ) as r:
                return r.status < 400
    except Exception:
        return False


async def telemetry_loop():
    """Background loop. Silent: no logs, no DB error writes, no UI surface."""
    while True:
        try:
            await asyncio.sleep(300)  # check every 5 min
            settings = await database.get_telemetry_settings()
            if not settings.get("enabled"):
                continue
            now = datetime.utcnow()
            nxt = settings.get("next_send_at")
            if nxt and nxt > now:
                continue
            ok = await _send_silently()
            # Schedule next send: full interval on success, 1h on failure.
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
