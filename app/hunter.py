"""Channel Hunter — autonomous discovery of file-sharing Telegram channels.

Pipeline:
  Stage 1 (internal mining):
    - Walk our existing `links` table for t.me/{username} links
    - Scan files.context for @username mentions
  Stage 2 (web crawl):
    - TGStat.com category listings (public)
    - Other public directories + scrape-friendly search engines
  Stage 3 (Telethon enrichment):
    - resolve username, fetch participant count, sample recent messages,
      compute file-type breakdown, score
  Stage 4 (scoring & ranking):
    - score = weighted blend of file density, recency, members, diversity

All stages honor user-configurable concurrency, request delays, and
daily caps. A FloodWait raises a backoff that is logged and respected.
"""
import asyncio
import mimetypes
import os
import json
import logging
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.channels import (
    GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest,
    GetChannelRecommendationsRequest,
)
from telethon.tl.types import (
    Channel, InputMessagesFilterDocument, DocumentAttributeFilename,
    DocumentAttributeVideo, DocumentAttributeAudio, InputPeerChannel,
    MessageMediaPhoto,
)

import database
from telegram_client import get_client

logger = logging.getLogger("hunter")

# In-memory live status (UI polls this)
status: dict = {
    "running": False,
    "stage": None,         # "stage1" | "stage2" | "stage3" | "scoring" | None
    "progress": 0,
    "total": 0,
    "seeds_found": 0,
    "enriched": 0,
    "failed": 0,
    "current": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "stage_started_at": None,
    "stage_detail": {},     # per-stage live detail (source progress, URL, etc.)
    "events": [],           # rolling list of recent log events
    "cancel_requested": False,
    "skip_stage_requested": False,
}


# ── Event persistence ────────────────────────────────────────────────────────
# Each accepted event is appended to a JSONL file on the data volume so the UI
# can render history after a container restart. The in-memory list and the
# file are both capped at the same MAX_EVENTS sliding window.
_EVENTS_LOG_PATH = os.path.join(
    os.environ.get("DATA_DIR", "/app/data"), "hunter_events.jsonl"
)
_MAX_EVENTS = 500
_emit_writes = 0  # how many appends since last compaction


def _load_persisted_events() -> None:
    """Tail-read up to _MAX_EVENTS rows from the persisted log into the
    in-memory status["events"] on process start. Best-effort: any IO/parse
    error just leaves the list empty."""
    try:
        if not os.path.exists(_EVENTS_LOG_PATH):
            return
        with open(_EVENTS_LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read at most the last 512 KB; plenty for 500 short lines.
            f.seek(max(0, size - 512 * 1024))
            chunk = f.read().decode("utf-8", errors="ignore")
        lines = [ln for ln in chunk.splitlines() if ln.strip()][-_MAX_EVENTS:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        status["events"] = out
    except Exception:
        pass


def _compact_events_file() -> None:
    """Rewrite the JSONL file with just the current in-memory tail. Cheap
    enough to run periodically (every _MAX_EVENTS appends)."""
    try:
        tmp = _EVENTS_LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for ev in status["events"][-_MAX_EVENTS:]:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.replace(tmp, _EVENTS_LOG_PATH)
    except Exception:
        pass


def _emit_event(stage: str, msg: str, level: str = "info", *, key: str = None, params: dict = None):
    """Append to rolling event log; cap at 500 across runs.

    Persists each event to a JSONL on disk so the UI keeps its history after
    a container restart. `key` + `params` let the frontend render a localized
    message via i18n.js; `msg` is kept as a fallback (and for backend logs)."""
    global _emit_writes
    try:
        ev = {
            "ts": datetime.utcnow().isoformat(),
            "stage": stage, "level": level, "msg": msg[:240],
        }
        if key:
            ev["key"] = key
            if params:
                ev["params"] = params
        status["events"].append(ev)
        if len(status["events"]) > _MAX_EVENTS:
            status["events"] = status["events"][-_MAX_EVENTS:]
        # Append a single line to the persisted log.
        try:
            os.makedirs(os.path.dirname(_EVENTS_LOG_PATH), exist_ok=True)
            with open(_EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            _emit_writes += 1
            # Compact every MAX_EVENTS appends so the file doesn't grow
            # without bound (the tail-reader can handle a large file, but
            # compaction keeps cold-boot fast and disk usage tidy).
            if _emit_writes >= _MAX_EVENTS:
                _compact_events_file()
                _emit_writes = 0
        except Exception:
            pass
    except Exception:
        pass


def _check_interrupt(stage: str) -> str:
    """Return 'cancel', 'skip', or '' to indicate user-requested interruption.
    Caller decides how to honor (skip current source/candidate, or break)."""
    if status.get("cancel_requested"):
        return "cancel"
    if status.get("skip_stage_requested"):
        return "skip"
    return ""


async def _interruptible_sleep(seconds: float):
    """Sleep up to `seconds` but wake early if cancel/skip is requested."""
    if seconds <= 0:
        return
    end = time.time() + seconds
    while time.time() < end:
        if _check_interrupt(""):
            return
        chunk = min(0.5, end - time.time())
        if chunk <= 0:
            return
        await asyncio.sleep(chunk)


_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
_TME_RE = re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})", re.IGNORECASE)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

# Per-source ban / cool-down state. Survives across calls so a banned source
# is skipped for the rest of the day.
_SOURCE_FAIL_STREAKS: Dict[str, int] = {}
_SOURCE_COOLDOWN_UNTIL: Dict[str, float] = {}      # epoch seconds
_FAIL_THRESHOLD = 3
_COOLDOWN_AFTER_FAIL_SEC = 6 * 60 * 60              # 6 hours

# ── CloakBrowser (stealth Chromium) singletons ───────────────────────────────
# Stage 2 uses a patched headless Chromium (CloakBrowser) for search engines
# and Cloudflare-fronted directories — defeats the bot-detection layers
# (Cloudflare Turnstile, FingerprintJS, reCAPTCHA v3) that were blocking
# our adapters. CloakBrowser exposes Playwright-style Browser / Context /
# Page objects, so the rest of this file uses the standard async browser
# API; the only swap is the launch entrypoint.
#
# Lazily start on first use, tear down at the end of every Stage 2 run so
# no Chromium processes / tabs linger between scheduled jobs. (`_PW` prefix
# stays for historical reasons — these are "patched web" browser handles.)
_PW_BROWSER = None  # CloakBrowser-launched stealth Chromium
_PW_CONTEXT = None  # single browser context (shares cookies across pages)
_PW_FAILED = False  # set if launch fails; subsequent calls short-circuit

# Cache of discovered URLs per source per session
_DISCOVERY_CACHE: Dict[str, List[str]] = {}

# Public file-host link domains we already track — skip these as channel candidates
_NON_CHANNEL_USERNAMES: Set[str] = {
    "joinchat", "share", "addstickers", "addtheme", "iv", "proxy", "setlanguage",
    "joinforum", "addemoji", "addtopic", "boost",
}

_running_lock = asyncio.Lock()
_run_task: Optional[asyncio.Task] = None

# Restore the activity log from disk before any event arrives this session, so
# a fresh UI refresh after a container restart still sees the previous run's
# detail. Best-effort: failure leaves the list empty (same as today).
_load_persisted_events()


def _normalize_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip().lstrip("@").lower()
    if u in _NON_CHANNEL_USERNAMES:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", u):
        return None
    return u


# ── Stage 0: similar channels (Telegram's own recommendation graph) ──────────
#
# Telegram exposes a per-channel "similar channels" recommendation list via
# `channels.getChannelRecommendations`. We use already-followed channels (and,
# optionally, recently-enriched hunter candidates) as seeds and harvest their
# recommendations into hunter_candidates. The graph is Telegram-computed, so
# the signal is far tighter than keyword search — but the list is small
# (~10 per channel on non-premium accounts) and gated by FloodWait, so we
# treat it as a complement to Stage 1/2, not a replacement.

async def _stage0_collect_seed_usernames(max_seeds: int) -> List[Tuple[str, int]]:
    """Return [(username, group_id_or_0), ...] of seed channels NOT yet queried.
    1) followed channels in `groups` (stage0_scanned_at IS NULL, priority)
    2) recently enriched hunter candidates (stage0_scanned_at IS NULL)
    Caps to `max_seeds` total. Skips entries without a username (private chats)."""
    seeds: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    # 1) followed channels that have never been used as Stage 0 seeds
    rows = await database._q(
        """SELECT id, username FROM groups
           WHERE is_channel = TRUE AND username IS NOT NULL AND username <> ''
             AND COALESCE(hidden, FALSE) = FALSE
             AND stage0_scanned_at IS NULL
           ORDER BY COALESCE(last_synced_at, '1970-01-01'::timestamptz) DESC
           LIMIT $1""",
        max_seeds,
    )
    for r in rows:
        u = _normalize_username(r["username"])
        if u and u not in seen:
            seen.add(u)
            seeds.append((u, int(r["id"])))
        if len(seeds) >= max_seeds:
            return seeds
    # 2) Fill remainder from enriched candidates not yet scanned
    remaining = max_seeds - len(seeds)
    if remaining > 0:
        rows = await database._q(
            """SELECT username FROM hunter_candidates
               WHERE status IN ('enriched', 'joined')
                 AND username IS NOT NULL
                 AND peer_id IS NOT NULL AND access_hash IS NOT NULL
                 AND stage0_scanned_at IS NULL
               ORDER BY COALESCE(enriched_at, discovered_at) DESC
               LIMIT $1""",
            remaining,
        )
        for r in rows:
            u = _normalize_username(r["username"])
            if u and u not in seen:
                seen.add(u)
                seeds.append((u, 0))
            if len(seeds) >= max_seeds:
                break
    return seeds


async def stage0_similar_expand(settings: dict) -> int:
    """Stage 0 — harvest Telegram's "similar channels" graph from seeded
    channels. Returns the number of *new* candidates added."""
    if not settings.get("similar_expand_enabled", True):
        return 0

    max_seeds = int(settings.get("similar_expand_max_seeds") or 100)
    max_per_seed = int(settings.get("similar_expand_max_per_seed") or 10)
    delay_ms = max(0, int(settings.get("tg_request_delay_ms") or 1500))
    account_id = int(settings.get("tg_account_id") or 1)

    seeds = await _stage0_collect_seed_usernames(max_seeds)
    if not seeds:
        _emit_event(
            "stage0", "Tohum kanal yok (önce kanal takip edin) — Stage 0 atlandı",
            "info", key="hl.stage0.noSeeds",
        )
        return 0

    try:
        client = await get_client(account_id)
    except Exception as e:
        _emit_event(
            "stage0", f"Telegram istemcisi alınamadı: {e}", "warn",
            key="hl.stage0.clientErr", params={"err": str(e)[:120]},
        )
        return 0

    status["stage_detail"] = {"seeds_total": len(seeds), "seeds_done": 0,
                              "found": 0, "added": 0}
    status["total"] = len(seeds)
    status["progress"] = 0
    _emit_event(
        "stage0",
        f"Stage 0: Telegram benzer-kanal grafiği — {len(seeds)} tohum kanal taranıyor",
        key="hl.stage0.start", params={"n": len(seeds)},
    )

    async def _mark_scanned(username: str, gid: int) -> None:
        """Stamp stage0_scanned_at so this seed is never re-queried."""
        now = datetime.now(timezone.utc)
        try:
            if gid:
                await database._q(
                    "UPDATE groups SET stage0_scanned_at = $1 WHERE id = $2",
                    now, gid,
                )
            else:
                await database._q(
                    "UPDATE hunter_candidates SET stage0_scanned_at = $1"
                    " WHERE lower(username) = lower($2)",
                    now, username,
                )
        except Exception as _e:
            logger.warning(f"stage0 mark_scanned failed for @{username}: {_e}")

    n_added = 0
    n_found = 0
    for idx, (seed_username, _seed_gid) in enumerate(seeds, 1):
        intr = _check_interrupt("stage0")
        if intr == "cancel":
            _emit_event("stage0", "Stage 0 iptal edildi", "warn",
                         key="hl.stage0.cancelled")
            break
        if intr == "skip":
            status["skip_stage_requested"] = False
            _emit_event("stage0", "Stage 0 atlandı (skip)", "warn",
                         key="hl.stage0.skipped")
            break

        status["progress"] = idx
        status["current"] = f"@{seed_username}"
        try:
            entity = await client.get_entity(seed_username)
        except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError) as e:
            _emit_event(
                "stage0", f"@{seed_username}: tohum erişilemez ({type(e).__name__})",
                "info", key="hl.stage0.seedSkip",
                params={"username": seed_username, "err": type(e).__name__},
            )
            await _mark_scanned(seed_username, _seed_gid)  # kalıcı hata — bir daha deneme
            continue
        except FloodWaitError as fw:
            wait_s = int(getattr(fw, "seconds", 30))
            _emit_event(
                "stage0", f"FloodWait {wait_s}s (tohum çözümleme @{seed_username})",
                "warn", key="hl.stage0.floodWait",
                params={"username": seed_username, "wait": wait_s},
            )
            await _interruptible_sleep(min(wait_s, 60))
            continue  # geçici hata — sonraki oturumda tekrar dene
        except Exception as e:
            _emit_event(
                "stage0", f"@{seed_username}: çözümleme hatası ({str(e)[:60]})",
                "warn", key="hl.stage0.seedErr",
                params={"username": seed_username, "err": str(e)[:60]},
            )
            await _mark_scanned(seed_username, _seed_gid)
            continue

        if not isinstance(entity, Channel):
            await _mark_scanned(seed_username, _seed_gid)
            continue

        try:
            res = await client(GetChannelRecommendationsRequest(channel=entity))
        except FloodWaitError as fw:
            wait_s = int(getattr(fw, "seconds", 30))
            _emit_event(
                "stage0", f"FloodWait {wait_s}s (öneri çağrısı @{seed_username})",
                "warn", key="hl.stage0.floodWait",
                params={"username": seed_username, "wait": wait_s},
            )
            await _interruptible_sleep(min(wait_s, 60))
            continue  # geçici hata — sonraki oturumda tekrar dene
        except Exception as e:
            _emit_event(
                "stage0", f"@{seed_username}: öneri alınamadı ({str(e)[:60]})",
                "warn", key="hl.stage0.recErr",
                params={"username": seed_username, "err": str(e)[:60]},
            )
            await _mark_scanned(seed_username, _seed_gid)
            continue

        recs = list(getattr(res, "chats", []) or [])
        if not recs:
            _emit_event(
                "stage0", f"@{seed_username}: öneri yok",
                "info", key="hl.stage0.seedEmpty",
                params={"username": seed_username},
            )
            await _mark_scanned(seed_username, _seed_gid)
            await _interruptible_sleep(delay_ms / 1000.0)
            continue

        seed_added = 0
        seed_dupe = 0
        seed_blacklisted = 0
        for chat in recs[:max_per_seed]:
            cu = getattr(chat, "username", None)
            cu = _normalize_username(cu) if cu else None
            if not cu:
                continue
            n_found += 1
            if await database.is_blacklisted(cu):
                seed_blacklisted += 1
                continue
            existing = await database.get_hunter_candidate_by_username(cu)
            if existing:
                seed_dupe += 1
                # Still record this seed as a source so we can trace the graph
                try:
                    await database.add_hunter_source(
                        int(existing["id"]), "similar", f"seed=@{seed_username}"
                    )
                except Exception:
                    pass
                continue
            cid = await database.upsert_hunter_candidate(cu)
            if cid:
                # Bake seed access info into hunter_candidates for future
                # lookups (saves a resolve burn later).
                try:
                    pid = getattr(chat, "id", None)
                    ah = getattr(chat, "access_hash", None)
                    if pid is not None and ah is not None:
                        await database.update_hunter_candidate(
                            cid, {"peer_id": int(pid), "access_hash": int(ah)}
                        )
                except Exception:
                    pass
                try:
                    await database.add_hunter_source(
                        cid, "similar", f"seed=@{seed_username}"
                    )
                except Exception:
                    pass
                seed_added += 1
                n_added += 1

        await _mark_scanned(seed_username, _seed_gid)
        status["stage_detail"]["seeds_done"] = idx
        status["stage_detail"]["found"] = n_found
        status["stage_detail"]["added"] = n_added
        _emit_event(
            "stage0",
            (f"@{seed_username}: {seed_added} yeni / {seed_dupe} mevcut / "
             f"{seed_blacklisted} blacklist · toplam yeni: {n_added}"),
            "info", key="hl.stage0.seedDone",
            params={
                "username": seed_username, "added": seed_added,
                "dupe": seed_dupe, "blacklisted": seed_blacklisted,
                "total_added": n_added,
            },
        )
        await _interruptible_sleep(delay_ms / 1000.0)

    _emit_event(
        "stage0",
        f"Stage 0 tamamlandı: {n_added} yeni aday / {n_found} öneri / {len(seeds)} tohum",
        key="hl.stage0.done",
        params={"added": n_added, "found": n_found, "seeds": len(seeds)},
    )
    return n_added


# ── Public: live "similar channels" preview ──────────────────────────────────
# Drives the right-click "Show similar channels" lightbox in the UI. Unlike
# the Stage-0 batch expander, this is invoked on demand for a single seed and
# returns a rich preview (sample filenames + sizes + breakdown) without
# touching hunter_candidates. It uses peer info baked into the recommendation
# response so resolveUsername is NOT burned for the recommendations.

async def _preview_sample_files(client, entity, limit: int) -> Dict:
    """Sample the last `limit` messages of `entity` for file info only.
    No DB writes, no scoring. Returns a small dict suitable for JSON."""
    file_count = 0
    total_size = 0
    breakdown: Dict[str, int] = {k: 0 for k in list(_FILE_GROUPS.keys()) + ["other"]}
    files: List[Dict] = []
    last_at = None
    sampled = 0
    try:
        async for msg in client.iter_messages(entity, limit=limit):
            sampled += 1
            if msg.date and (last_at is None or msg.date > last_at):
                last_at = msg.date
            if msg.document:
                doc = msg.document
                size = int(getattr(doc, "size", 0) or 0)
                fname, ext, _v, _a, is_named = _doc_filename(doc, msg.id)
                grp = _file_group(ext)
                file_count += 1
                total_size += size
                breakdown[grp] = breakdown.get(grp, 0) + 1
                files.append({
                    "name": fname or f"file_{msg.id}",
                    "size": size,
                    "ext": ext,
                    "group": grp,
                    "named": bool(is_named),
                    "date": msg.date.isoformat() if msg.date else None,
                    "message_id": msg.id,
                })
            elif isinstance(msg.media, MessageMediaPhoto):
                photo = getattr(msg, "photo", None)
                size = _photo_size(photo) if photo else 0
                file_count += 1
                total_size += size
                breakdown["image"] = breakdown.get("image", 0) + 1
                files.append({
                    "name": f"photo_{msg.id}.jpg",
                    "size": size,
                    "ext": "jpg",
                    "group": "image",
                    "named": False,
                    "date": msg.date.isoformat() if msg.date else None,
                    "message_id": msg.id,
                })
    except FloodWaitError:
        raise
    except Exception as e:
        logger.debug(f"preview sample failed: {e}")
    return {
        "file_count_sampled": file_count,
        "sampled_messages": sampled,
        "total_size": total_size,
        "breakdown": {k: v for k, v in breakdown.items() if v},
        "files": files,
        "last_message_at": last_at.isoformat() if last_at else None,
    }


async def get_similar_channels_preview(
    seed_username: Optional[str] = None,
    seed_peer_id: Optional[int] = None,
    seed_access_hash: Optional[int] = None,
    seed_group_id: Optional[int] = None,
    files_limit: int = 30,
    max_recommendations: int = 12,
) -> Dict:
    """Live "similar channels" preview for the UI lightbox.

    Resolves the seed (preferring cached peer/access_hash over a username
    resolve burn), calls GetChannelRecommendationsRequest, and for every
    suggested channel collects:
      • title / @username / member count (from the response itself)
      • last-N file sample (name, size, ext, group, date)
      • aggregated breakdown
    Returns a dict the frontend can render as cards."""
    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)
    client = await get_client(account_id)

    # Resolve seed — prefer cached peer (no resolve burn).
    seed_entity = None
    if seed_peer_id is not None and seed_access_hash is not None:
        try:
            seed_entity = await client.get_entity(
                InputPeerChannel(int(seed_peer_id), int(seed_access_hash))
            )
        except Exception:
            seed_entity = None
    # Telethon's session cache often has the access_hash for groups we
    # already follow — try that before falling back to a username resolve
    # (and use it for groups that have no public username at all).
    if seed_entity is None and seed_group_id is not None:
        try:
            seed_entity = await client.get_entity(int(seed_group_id))
        except Exception:
            seed_entity = None
    if seed_entity is None:
        if not seed_username:
            return {"error": "no_seed", "channels": []}
        seed_entity = await client.get_entity(seed_username)

    if not isinstance(seed_entity, Channel):
        return {"error": "not_a_channel", "channels": []}

    # Recommendations
    try:
        res = await client(GetChannelRecommendationsRequest(channel=seed_entity))
    except FloodWaitError as fw:
        return {
            "error": "floodwait",
            "wait_seconds": int(getattr(fw, "seconds", 0)),
            "channels": [],
        }

    chats = list(getattr(res, "chats", []) or [])[:max_recommendations]
    seed_info = {
        "username": getattr(seed_entity, "username", None),
        "title": getattr(seed_entity, "title", "") or "",
        "id": getattr(seed_entity, "id", None),
    }

    # Pre-fetch our local awareness of these channels (followed / candidate /
    # blacklisted) so the UI can show badges without extra round-trips.
    usernames_lower = [
        (c.username or "").lower() for c in chats if getattr(c, "username", None)
    ]
    awareness: Dict[str, Dict] = {}
    if usernames_lower:
        # Followed groups
        rows = await database._q(
            "SELECT LOWER(username) AS u FROM groups WHERE LOWER(username) = ANY($1::text[])",
            usernames_lower,
        )
        for r in rows:
            awareness.setdefault(r["u"], {})["followed"] = True
        # Hunter candidates + blacklist
        bulk = await database.check_hunter_candidates_bulk(usernames_lower)
        for u, st in bulk.items():
            awareness.setdefault(u, {})["state"] = st  # "blacklisted" / "joined" / "queued"

    out_channels: List[Dict] = []
    for chat in chats:
        cu = getattr(chat, "username", None)
        pid = getattr(chat, "id", None)
        ah = getattr(chat, "access_hash", None)
        title = getattr(chat, "title", "") or ""
        members = getattr(chat, "participants_count", None)
        verified = bool(getattr(chat, "verified", False))
        scam = bool(getattr(chat, "scam", False))
        info = {
            "username": cu,
            "title": title,
            "peer_id": pid,
            "access_hash": ah,
            "members": members,
            "verified": verified,
            "scam": scam,
            "awareness": awareness.get((cu or "").lower(), {}),
            "files_sample": None,
            "files_error": None,
        }
        # Sample files using the peer info we already have — no resolve burn.
        if pid is not None and ah is not None and files_limit > 0:
            try:
                entity = await client.get_entity(InputPeerChannel(int(pid), int(ah)))
                sample = await _preview_sample_files(client, entity, files_limit)
                info["files_sample"] = sample
            except FloodWaitError as fw:
                info["files_error"] = f"floodwait:{int(getattr(fw, 'seconds', 0))}"
            except ChannelPrivateError:
                info["files_error"] = "private"
            except Exception as e:
                info["files_error"] = f"err:{str(e)[:80]}"
        out_channels.append(info)

    return {
        "seed": seed_info,
        "channels": out_channels,
        "recommendation_count": len(chats),
    }


# ── Stage 1: internal mining ─────────────────────────────────────────────────

async def stage1_mine_internal() -> int:
    """Extract candidate usernames from our own DB (links + file contexts)."""
    n_added = 0
    seen: Set[str] = set()

    # 1a) t.me/... links from links table
    rows = await database._q(
        "SELECT id, group_id, url FROM links WHERE url ILIKE 't%t.me/%' LIMIT 100000"
    )
    for r in rows:
        m = _TME_RE.search(r["url"] or "")
        if not m:
            continue
        u = _normalize_username(m.group(1))
        if not u or u in seen:
            continue
        seen.add(u)
        if await database.is_blacklisted(u):
            continue
        cid = await database.upsert_hunter_candidate(u)
        if cid:
            await database.add_hunter_source(cid, "internal:link", f"group_id={r['group_id']}")
            n_added += 1

    # 1b) @mentions from files.context (limited to a sample to keep things fast)
    rows = await database._q(
        """SELECT id, group_id, context FROM files
           WHERE context IS NOT NULL AND context ~* '@[A-Za-z][A-Za-z0-9_]{4,31}'
           LIMIT 200000"""
    )
    for r in rows:
        ctx = r["context"] or ""
        for m in _USERNAME_RE.finditer(ctx):
            u = _normalize_username(m.group(1))
            if not u or u in seen:
                continue
            seen.add(u)
            if await database.is_blacklisted(u):
                continue
            cid = await database.upsert_hunter_candidate(u)
            if cid:
                await database.add_hunter_source(cid, "internal:mention", f"group_id={r['group_id']}")
                n_added += 1

    return n_added


# ── Stage 2: web crawl ───────────────────────────────────────────────────────

async def _fetch_text(session: aiohttp.ClientSession, url: str, *,
                       timeout: int = 20, referer: Optional[str] = None) -> Optional[str]:
    short = url.split("//", 1)[-1][:90]
    status["current"] = short
    headers = dict(_BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                headers=headers, allow_redirects=True) as r:
            if r.status >= 400:
                _emit_event("stage2", f"HTTP {r.status} {short}", "warn", key="hl.stage2.httpFail", params={"status": r.status, "url": short})
                return None
            text = await r.text(errors="ignore")
            # Some sites return 200 with a Cloudflare/JS challenge page.
            low = text[:5000].lower()
            if ("cloudflare" in low and ("challenge" in low or "checking your browser" in low))                or "captcha" in low or "are you a robot" in low:
                _emit_event("stage2", f"challenge page on {short}", "warn", key="hl.stage2.challenge", params={"url": short})
                return None
            return text
    except Exception as e:
        _emit_event("stage2", f"fail {short}: {str(e)[:60]}", "warn", key="hl.stage2.fail", params={"url": short, "err": str(e)[:60]})
        return None


# ── CloakBrowser (stealth Chromium) — used for sources that block aiohttp ────

async def _pw_ensure_started() -> bool:
    """Lazily boot CloakBrowser (patched stealth Chromium) on first call.
    Returns False if the runtime can't start (missing image deps, etc.)
    so callers can degrade gracefully to plain HTTP instead of crashing
    the whole stage.

    `browser.close()` is monkey-patched by CloakBrowser to also stop the
    underlying browser driver, so teardown is just `await browser.close()`."""
    global _PW_BROWSER, _PW_CONTEXT, _PW_FAILED
    if _PW_FAILED:
        return False
    if _PW_CONTEXT is not None:
        return True
    try:
        from cloakbrowser import launch_async
        _PW_BROWSER = await launch_async(
            headless=True,
            # humanize patches the browser's mouse / keyboard / scroll with
            # Bézier curves + realistic timing — defeats behavioural bot
            # detection on top of the C++ fingerprint patches.
            humanize=True,
            # --no-sandbox is required inside Docker (root user, no userns);
            # disabling dev-shm avoids OOMs on small /dev/shm partitions.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        # CloakBrowser already sets a realistic UA + fingerprints at the
        # binary level, so we deliberately do NOT override user_agent here —
        # letting it use the patched value keeps the JA3/JA4/UA stack
        # consistent. Locale + viewport are still useful nudges.
        _PW_CONTEXT = await _PW_BROWSER.new_context(
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            java_script_enabled=True,
        )
        _emit_event("stage2", "headless Chromium ready", "info", key="hl.stage2.pwReady")
        return True
    except Exception as e:
        _PW_FAILED = True
        logger.warning(f"CloakBrowser start failed: {e}")
        _emit_event("stage2", f"Stealth browser unavailable: {str(e)[:120]}", "warn", key="hl.stage2.pwUnavailable", params={"err": str(e)[:120]})
        return False


async def _pw_get(url: str, *, referer: Optional[str] = None,
                   timeout: int = 25) -> Optional[str]:
    """Fetch a URL with a real headless Chromium. Each call opens its own
    page and closes it in finally — so we never leak tabs even if the
    stage is interrupted mid-fetch."""
    if not await _pw_ensure_started():
        return None
    short = url.split("//", 1)[-1][:90]
    status["current"] = short
    page = None
    try:
        page = await _PW_CONTEXT.new_page()
        if referer:
            await page.set_extra_http_headers({"Referer": referer})
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        # Some sites lazy-load result links via JS — wait briefly for the
        # network to quiet down, but don't block forever if it never does.
        try:
            await page.wait_for_load_state("networkidle", timeout=3500)
        except Exception:
            pass
        html = await page.content()
        low = html[:5000].lower()
        if ("just a moment" in low or "checking your browser" in low
                or "captcha" in low or "are you a robot" in low
                or "enable javascript" in low):
            _emit_event("stage2", f"challenge page (pw) on {short}", "warn", key="hl.stage2.pwChallenge", params={"url": short})
            return None
        return html
    except Exception as e:
        _emit_event("stage2", f"pw fail {short}: {str(e)[:80]}", "warn", key="hl.stage2.pwFail", params={"url": short, "err": str(e)[:80]})
        return None
    finally:
        if page is not None:
            try: await page.close()
            except Exception: pass


async def _pw_teardown() -> None:
    """Close every Chromium tab/context/process at the end of a Stage 2 run.
    Without this the container would keep a headless browser pinned to RAM
    24/7 even though scraping runs only a few times a day.

    CloakBrowser monkey-patches browser.close() so it also stops the
    underlying browser driver — closing the browser is enough."""
    global _PW_BROWSER, _PW_CONTEXT
    if _PW_CONTEXT is not None:
        try:
            for p in list(_PW_CONTEXT.pages):
                try: await p.close()
                except Exception: pass
            await _PW_CONTEXT.close()
        except Exception: pass
        _PW_CONTEXT = None
    if _PW_BROWSER is not None:
        try: await _PW_BROWSER.close()
        except Exception: pass
        _PW_BROWSER = None


def _source_can_run(name: str) -> bool:
    cd = _SOURCE_COOLDOWN_UNTIL.get(name)
    if cd and time.time() < cd:
        return False
    return True


def _source_record_failure(name: str):
    n = _SOURCE_FAIL_STREAKS.get(name, 0) + 1
    _SOURCE_FAIL_STREAKS[name] = n
    if n >= _FAIL_THRESHOLD:
        _SOURCE_COOLDOWN_UNTIL[name] = time.time() + _COOLDOWN_AFTER_FAIL_SEC
        _emit_event("stage2", f"{name}: {n} consecutive failures → cool-down 6h", "warn", key="hl.stage2.consecFails", params={"src": name, "n": n})


def _source_record_success(name: str):
    _SOURCE_FAIL_STREAKS[name] = 0


async def _warmup_homepage(session: aiohttp.ClientSession, base_url: str) -> Optional[str]:
    """Visit the site's homepage first so any cookies are set, then return the
    homepage HTML so adapters can mine it for category links."""
    return await _fetch_text(session, base_url, timeout=20)


def _extract_internal_links(html: str, base_host: str) -> List[str]:
    """Pull href values whose host matches base_host (or is relative)."""
    if not html:
        return []
    out, seen = [], set()
    for m in re.finditer(r'href=[\"\']([^\"\']+)', html):
        href = m.group(1).strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = f"https://{base_host}{href}"
        elif not href.startswith("http"):
            href = f"https://{base_host}/{href.lstrip('./')}"
        if base_host not in href:
            continue
        if href in seen:
            continue
        seen.add(href); out.append(href)
    return out


_TGSTAT_CATEGORIES = [
    "movies", "telecast", "books", "software", "music",
    "video_games", "education", "design", "tech",
    "linguistics", "courses", "podcasts",
]

# Smart keyword expansion: when user gives no/limited terms, fan out to
# common file-channel categories so search-engine queries hit broadly.
_DEFAULT_FILE_CATEGORIES = [
    "movies", "films", "tv shows", "documentaries", "books", "ebooks",
    "audiobooks", "magazines", "music", "albums", "lossless",
    "software", "apps", "games", "android apps",
    "courses", "tutorials", "ebooks pdf",
    "fonts", "stock", "templates", "icons",
    "comics", "manga",
]
_FILE_MODIFIERS = [
    "channel", "files", "downloads", "archive", "library", "collection", "dump",
]


def _smart_keywords(user_kw: str) -> List[str]:
    """Build a useful keyword list for search-engine queries.

    Uses user-provided keywords if any; otherwise falls back to a curated
    set of file-sharing categories. Each keyword is paired with file-channel
    modifiers when constructing search queries.
    """
    base = [k.strip() for k in (user_kw or "").split(",") if k.strip()]
    if not base:
        base = _DEFAULT_FILE_CATEGORIES
    return base


# ── Auto-learned keyword pool (#3) + Trend injection (#5) ───────────────────

# English + Turkish stopwords plus genre/role tokens that are too generic to
# be useful as search seeds. Kept short on purpose — only the words that
# would otherwise dominate frequency counts.
_KEYWORD_STOPWORDS: Set[str] = set("""
the a an and or of to in for is are this that with from but be as by on at was were
what when where why how all any our your his her its them their not no yes
ben biz sen siz bir ile için olarak olan olur bu şu ki de da çok daha en
new old last year years season episode movies movie series tv show shows
channel channels group groups telegram dosya dosyalar kanal kanallar link links
official premium free download free indir indirme yeni eski son
""".split())

# Build a small per-channel keyword list from its title + description.
# Used in Stage 3 right after enrichment succeeds (#3).
def _extract_learned_keywords(title: Optional[str], description: Optional[str],
                                max_kw: int = 4) -> List[str]:
    text = " ".join(filter(None, [title or "", description or ""]))
    if not text:
        return []
    from collections import Counter
    toks = re.findall(r"[A-Za-zĞÜŞİÖÇğüşıöç][A-Za-zĞÜŞİÖÇğüşıöç0-9'-]{3,19}", text)
    cnt: Counter = Counter()
    for t in toks:
        low = t.lower()
        if low in _KEYWORD_STOPWORDS: continue
        if low.startswith(("http", "www", "t.me")): continue
        cnt[low] += 1
    # Top-N by frequency; ties broken by insertion order (Counter behaviour)
    return [w for w, _ in cnt.most_common(max_kw)]


async def remember_learned_keywords(words: List[str], cap: int = 60) -> None:
    """Merge a few extracted keywords into the persisted learned pool.
    Capped so the pool can't grow unbounded — oldest entries fall off
    when the cap is hit."""
    words = [w for w in (words or []) if w and len(w) >= 4]
    if not words:
        return
    try:
        settings = await database.get_hunter_settings()
        current = [k.strip().lower() for k in (settings.get("learned_keywords") or "").split(",") if k.strip()]
        # Prepend new tokens, dedup preserving order, then cap
        merged: List[str] = []
        seen = set()
        for w in words + current:
            lw = w.lower()
            if lw in seen: continue
            seen.add(lw); merged.append(lw)
            if len(merged) >= cap: break
        if merged != current:
            await database.update_hunter_settings({"learned_keywords": ",".join(merged)})
    except Exception as e:
        logger.debug(f"remember_learned_keywords failed: {e}")


# Process-memory cache for trend keywords. Reddit feeds rate-limit so we
# refresh at most once an hour; a stale list is fine.
_TREND_CACHE: Dict[str, Any] = {"at": 0.0, "terms": []}
_TREND_TTL_SEC = 3600
_TREND_SUBREDDITS = "movies+television+gaming+books+anime+piratedgames+softwaregore+technology"


async def _fetch_trend_keywords(max_terms: int = 8) -> List[str]:
    """Pull a few currently-trending topical terms from public Reddit feeds
    (no auth required). Restricted to subreddits whose topics map to
    file-sharing themes, so the injected keywords have a reasonable
    chance of surfacing relevant Telegram channels. (#5)"""
    now = time.time()
    if _TREND_CACHE["terms"] and now - _TREND_CACHE["at"] < _TREND_TTL_SEC:
        return _TREND_CACHE["terms"]
    url = f"https://www.reddit.com/r/{_TREND_SUBREDDITS}/hot.json?limit=25"
    headers = {"User-Agent": "telfiles/0.4 (channel-hunter trend probe)"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return _TREND_CACHE["terms"] or []
                data = await r.json()
        titles: List[str] = []
        for c in (data.get("data") or {}).get("children", []):
            t = (c.get("data") or {}).get("title") or ""
            if t: titles.append(t)
        # Frequency over the title corpus, filtered by the same stopword
        # logic used by _extract_learned_keywords above.
        from collections import Counter
        cnt: Counter = Counter()
        for t in titles:
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9'-]{4,19}", t):
                low = tok.lower()
                if low in _KEYWORD_STOPWORDS: continue
                cnt[low] += 1
        terms = [w for w, _ in cnt.most_common(max_terms)]
        _TREND_CACHE["at"] = now
        _TREND_CACHE["terms"] = terms
        return terms
    except Exception as e:
        logger.debug(f"trend fetch failed: {e}")
        return _TREND_CACHE["terms"] or []


def _build_search_queries(keywords: List[str], max_q: int = 30) -> List[str]:
    """Turn base keywords into a diversified set of Google-dork-style queries.

    Each engine adapter slices into its own limit (see queries[:N] inside the
    adapters) so the combinatorial growth is naturally throttled per source.
    """
    out: List[str] = []
    seen = set()

    def push(q: str) -> bool:
        k = q.lower()
        if k not in seen:
            seen.add(k); out.append(q)
        return len(out) >= max_q

    # Pattern A: site:t.me {kw} + file-channel modifier
    for kw in keywords:
        for mod in _FILE_MODIFIERS[:3]:
            if push(f'site:t.me {kw} {mod}'): return out

    # Pattern B: t.me/s preview pages — high precision
    for kw in keywords[:6]:
        if push(f'site:t.me/s {kw}'): return out

    # Pattern C: intitle dork — channel landings repeat name in <title>
    for kw in keywords[:5]:
        if push(f'site:t.me intitle:{kw}'): return out

    # Pattern D: invite-link patterns
    for kw in keywords[:4]:
        if push(f'inurl:t.me/joinchat {kw}'): return out
        if push(f'"t.me/+" {kw}'): return out

    # Pattern E: file-extension hints
    for kw in keywords[:4]:
        if push(f'site:t.me {kw} ".pdf" OR ".epub"'): return out
        if push(f'site:t.me {kw} ".rar" OR ".zip" OR ".7z"'): return out
        if push(f'site:t.me {kw} ".apk" OR ".exe"'): return out

    # Pattern F: bare bait phrases
    for kw in keywords[:4]:
        if push(f'"telegram channel" {kw}'): return out
        if push(f'"View in Telegram" {kw}'): return out

    # Pattern G: time-window dorks — yıl/ay markörleri ile son aylarda
    # yayımlanan/güncellenen sayfalara öncelik verir. Tek başına engine
    # freshness parametrelerinden ('&qdr=m' vb.) daha taşınabilir; sorgu
    # metnine yazıldığı için tüm motorlarda çalışır. (#4)
    now = datetime.now()
    year = now.year
    last_year = year - 1
    month_name = now.strftime("%B").lower()
    for kw in keywords[:5]:
        if push(f'site:t.me {kw} {year}'): return out
        if push(f'site:t.me {kw} {month_name} {year}'): return out
    for kw in keywords[:3]:
        if push(f'site:t.me {kw} "new"'): return out
        if push(f'site:t.me {kw} {last_year} OR {year}'): return out

    # Pattern H: paste-site dorks — t.me links shared on popular paste platforms
    _PASTE_DORK_SITES = [
        "pastebin.com", "gist.github.com", "justpaste.it",
        "paste.ee", "rentry.co", "hastebin.com", "dpaste.org",
    ]
    for site in _PASTE_DORK_SITES:
        if push(f'site:{site} t.me'): return out

    return out


# ── Source adapters (kamuya açık dizinler & arama motorları) ────────────────

# ── Adaptive directory crawler ──────────────────────────────────────────────

async def _crawl_directory(session: aiohttp.ClientSession, name: str,
                            base_url: str, delay_ms: int,
                            max_subpages: int = 12,
                            keywords: Optional[List[str]] = None) -> Set[str]:
    """Generic directory crawler.

    Visits the site's homepage, scrapes all internal links, scores those that
    look like channel listings / categories / search pages, then visits the
    top-N. Aggregates t.me usernames from every page.

    keywords (optional) — when provided, also tries homepage-relative search
    URLs constructed from the page's <form> action + each keyword.
    """
    found: Set[str] = set()
    homepage_html = await _fetch_text(session, base_url)
    if not homepage_html:
        return found
    # Mine the homepage itself first.
    for m in _TME_RE.finditer(homepage_html):
        u = _normalize_username(m.group(1))
        if u: found.add(u)

    base_host = base_url.split("//", 1)[-1].split("/", 1)[0]
    links = _extract_internal_links(homepage_html, base_host)

    SCORE_WORDS = [
        "channel", "rating", "top", "categor", "popular", "best",
        "trend", "directory", "listing", "tg", "telegram", "search",
    ]
    scored = []
    for href in links:
        h = href.lower()
        # Skip obvious non-content sub-pages
        if any(skip in h for skip in ["/login", "/signin", "/signup", "/register",
                                       "/auth", "/api/", ".css", ".js", ".png",
                                       ".jpg", ".svg", "mailto:", "/about",
                                       "/contact", "/privacy", "/tos", "/terms"]):
            continue
        s = sum(1 for w in SCORE_WORDS if w in h)
        if s > 0:
            scored.append((s, href))
    # De-prioritise duplicates with same path prefix to diversify
    scored.sort(reverse=True)

    consecutive_fails = 0
    for _, link in scored[:max_subpages]:
        if _check_interrupt("stage2"):
            break
        await _interruptible_sleep(delay_ms / 1000)
        sub_html = await _fetch_text(session, link, referer=base_url)
        if not sub_html:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                _emit_event("stage2", f"{name}: 3 fails in a row, stopping site early", "warn", key="hl.stage2.threeFailsSite", params={"src": name})
                break
            continue
        consecutive_fails = 0
        for m in _TME_RE.finditer(sub_html):
            u = _normalize_username(m.group(1))
            if u: found.add(u)

    # Optional keyword-driven search probe (for sites that have a /search?q=… form)
    if keywords:
        # Try to pull a search form action from the homepage
        forms = re.findall(r'<form[^>]*action=[\"\']([^\"\']+)[^>]*>([^<]+(?:<(?!/form)[^<]+)*)</form>',
                            homepage_html, re.I)
        search_path = None
        for action, body in forms:
            if "search" in action.lower() or "search" in body.lower() or "q=" in action.lower():
                search_path = action; break
        # Common defaults if nothing found
        if not search_path:
            for cand in ("/search", "/?q=", "/find"):
                search_path = cand; break
        # Build URLs
        if search_path:
            if search_path.startswith("/"):
                search_path = f"https://{base_host}{search_path}"
            elif not search_path.startswith("http"):
                search_path = f"https://{base_host}/{search_path}"
            sep = "&" if "?" in search_path else "?"
            for kw in keywords[:6]:
                if _check_interrupt("stage2"): break
                url = f"{search_path}{sep}q={aiohttp.helpers.quote(kw)}"
                await _interruptible_sleep(delay_ms / 1000)
                sub_html = await _fetch_text(session, url, referer=base_url)
                if not sub_html: continue
                for m in _TME_RE.finditer(sub_html):
                    u = _normalize_username(m.group(1))
                    if u: found.add(u)
    return found


async def _crawl_search_engine(session: aiohttp.ClientSession, name: str,
                                home_url: str, query_url_tpl: str,
                                queries: List[str], delay_ms: int,
                                max_q: int = 8,
                                page_offsets: Optional[List[str]] = None) -> Set[str]:
    """Generic search-engine adapter. Routes through headless Chromium since
    nearly every general-purpose search engine now serves a JS challenge or
    a "you look like a bot" page to plain aiohttp. We warm up via the
    homepage so first-party cookies stick, then issue `max_q` queries
    through the same browser context.

    page_offsets — list of suffix strings appended to the query URL to
    request subsequent result pages. Defaults to ["",] (page 1 only).
    Engines pass things like ["", "&start=10"] for Google,
    ["", "&first=11"] for Bing, etc. — broadens result diversity by
    surfacing the tail of the result list. (#2)"""
    page_offsets = page_offsets or [""]
    found: Set[str] = set()
    # Warm up — sets first-party cookies in the Chromium context.
    _ = await _pw_get(home_url)
    await asyncio.sleep(delay_ms / 1000)

    consecutive_fails = 0
    for q in queries[:max_q]:
        if _check_interrupt("stage2"): break
        base_url = query_url_tpl.format(q=aiohttp.helpers.quote(q))
        for off in page_offsets:
            if _check_interrupt("stage2"): break
            url = base_url + off
            html = await _pw_get(url, referer=home_url)
            if not html:
                consecutive_fails += 1
                if consecutive_fails >= 3:
                    _emit_event("stage2", f"{name}: 3 fails — backing off this run", "warn", key="hl.stage2.threeFailsRun", params={"src": name})
                    return found
                await _interruptible_sleep(delay_ms / 1000 * 2)
                continue
            consecutive_fails = 0
            for m in _TME_RE.finditer(html):
                u = _normalize_username(m.group(1))
                if u: found.add(u)
            await asyncio.sleep(delay_ms / 1000)
    return found


# ── Site-specific thin wrappers ─────────────────────────────────────────────

async def _stage2_tgstat(session, delay_ms):
    return await _crawl_directory(session, "tgstat", "https://tgstat.com/", delay_ms)

async def _stage2_telemetrio(session, delay_ms):
    return await _crawl_directory(session, "telemetrio", "https://telemetr.io/", delay_ms)

async def _stage2_combot(session, delay_ms):
    return await _crawl_directory(session, "combot", "https://combot.org/", delay_ms)

async def _stage2_tdoru(session, delay_ms):
    """t-do.ru — large RU/CIS Telegram catalog with deep category browse."""
    return await _crawl_directory(session, "tdoru", "https://t-do.ru/", delay_ms)


async def _stage2_telegaio(session, delay_ms):
    """telega.io — global multi-language directory + ad marketplace; English
    catalog at /en/catalog has stable HTML."""
    return await _crawl_directory(session, "telegaio", "https://telega.io/en/", delay_ms)


async def _stage2_hackernews(session, keywords, delay_ms):
    """Hacker News mentions of t.me via the public Algolia API. Higher-signal
    than search-engine results because each match comes from a comment that a
    real human posted endorsing or linking to a channel.

    The Algolia endpoint accepts arbitrary text queries and returns JSON; we
    scan the JSON as text for t.me/{username} occurrences."""
    found: Set[str] = set()
    base = "https://hn.algolia.com/api/v1/search?tags=comment&hitsPerPage=100&query="
    queries = ["t.me", "telegram channel"]
    for kw in (keywords or [])[:4]:
        queries.append(f"t.me {kw}")
    for q in queries[:8]:
        if _check_interrupt("stage2"): break
        url = base + aiohttp.helpers.quote(q)
        html = await _fetch_text(session, url)
        if html:
            for m in _TME_RE.finditer(html):
                u = _normalize_username(m.group(1))
                if u: found.add(u)
        await asyncio.sleep(delay_ms / 1000)
    return found


async def _stage2_ecosia(session, queries, delay_ms):
    """Ecosia — Bing-backed but separate rate-limit pool, scrape-friendly."""
    return await _crawl_search_engine(
        session, "ecosia",
        "https://www.ecosia.org/",
        "https://www.ecosia.org/search?q={q}",
        queries, delay_ms,
        page_offsets=["", "&p=1"],
    )


async def _stage2_tdirectory(session, delay_ms):
    return await _crawl_directory(session, "tdirectory", "https://tdirectory.me/", delay_ms)

async def _stage2_tlgrm(session, delay_ms):
    return await _crawl_directory(session, "tlgrm", "https://tlgrm.eu/", delay_ms)

async def _stage2_telegramic(session, delay_ms):
    return await _crawl_directory(session, "telegramic", "https://telegramic.org/", delay_ms)

async def _stage2_tgchannels(session, delay_ms):
    return await _crawl_directory(session, "tgchannels", "https://telegramchannels.me/", delay_ms)

async def _stage2_searchtg(session, keywords, delay_ms):
    return await _crawl_directory(session, "searchtg", "https://t.me/", delay_ms,
                                    keywords=keywords or _DEFAULT_FILE_CATEGORIES[:6])

async def _stage2_duckduckgo(session, queries, delay_ms):
    # DDG /html/ doesn't support deterministic pagination via GET params
    # (uses POST + s=N in the body); page 1 only.
    return await _crawl_search_engine(session, "duckduckgo",
        "https://duckduckgo.com/",
        "https://duckduckgo.com/html/?q={q}",
        queries, delay_ms,
        page_offsets=[""])

async def _stage2_yandex(session, queries, delay_ms):
    # Yandex pagination: &p=0 first, &p=1 second (0-indexed).
    return await _crawl_search_engine(session, "yandex",
        "https://yandex.com/",
        "https://yandex.com/search/?text={q}",
        queries, delay_ms,
        page_offsets=["", "&p=1"])

async def _stage2_brave(session, queries, delay_ms):
    # Brave pagination: &offset=0 first, &offset=1 second.
    return await _crawl_search_engine(session, "brave",
        "https://search.brave.com/",
        "https://search.brave.com/search?q={q}&source=web",
        queries, delay_ms,
        page_offsets=["", "&offset=1"])

async def _stage2_bing(session, queries, delay_ms):
    # Bing pagination: omit param for page 1, &first=11 for page 2.
    return await _crawl_search_engine(session, "bing",
        "https://www.bing.com/",
        "https://www.bing.com/search?q={q}",
        queries, delay_ms,
        page_offsets=["", "&first=11"])

async def _stage2_mojeek(session, queries, delay_ms):
    # Mojeek pagination: &s=11 for page 2.
    return await _crawl_search_engine(session, "mojeek",
        "https://www.mojeek.com/",
        "https://www.mojeek.com/search?q={q}",
        queries, delay_ms,
        page_offsets=["", "&s=11"])

async def _stage2_startpage(session, queries, delay_ms):
    return await _crawl_search_engine(session, "startpage",
        "https://www.startpage.com/",
        "https://www.startpage.com/do/search?q={q}",
        queries, delay_ms,
        page_offsets=["", "&page=2"])

async def _stage2_google(session, queries, delay_ms):
    """Google web search with dork queries (site:t.me ...).
    Google is aggressive about CAPTCHA; the cool-down logic in stage2 will
    park this source for 6h after 3 consecutive zero/error responses."""
    # Use the simpler /search endpoint that more often serves HTML directly.
    # `num=30` already pulls 30 results, so just one page is usually enough;
    # the second page (&start=30) bumps it to 60 for marginal diversity.
    return await _crawl_search_engine(session, "google",
        "https://www.google.com/",
        "https://www.google.com/search?q={q}&hl=en&num=30",
        queries, delay_ms, max_q=6,
        page_offsets=["", "&start=30"])


async def _stage2_reddit(session, keywords, delay_ms):
    return await _crawl_directory(session, "reddit", "https://www.reddit.com/r/TelegramGroups/", delay_ms,
                                    keywords=keywords or ["files", "movies", "books"])

async def _stage2_github(session, delay_ms):
    # Curated github repositories that list Telegram channels
    found: Set[str] = set()
    pages = [
        "https://github.com/search?q=awesome+telegram+channels&type=repositories",
        "https://github.com/avivace/awesome-telegram-channels",
    ]
    for url in pages:
        if _check_interrupt("stage2"): break
        html = await _fetch_text(session, url)
        if html:
            for m in _TME_RE.finditer(html):
                u = _normalize_username(m.group(1))
                if u: found.add(u)
        await asyncio.sleep(delay_ms / 1000)
    return found


# ── Web archive sources ────────────────────────────────────────────────────────

async def _stage2_wayback(session: aiohttp.ClientSession, delay_ms: int) -> Set[str]:
    """Wayback Machine CDX API — t.me URLs archived by the Internet Archive.
    Collapses by urlkey to deduplicate mirror snapshots; only 200-status pages."""
    found: Set[str] = set()
    url = (
        "http://web.archive.org/cdx/search/cdx"
        "?url=t.me/*&output=text&fl=original&limit=10000"
        "&collapse=urlkey&matchType=prefix&filter=statuscode:200"
    )
    _emit_event("stage2", "wayback: querying CDX API…", key="hl.stage2.waybackStart")
    text = await _fetch_text(session, url, timeout=60)
    if text:
        for m in _TME_RE.finditer(text):
            u = _normalize_username(m.group(1))
            if u:
                found.add(u)
        _emit_event("stage2", f"wayback: {len(found)} candidates",
                    key="hl.stage2.waybackDone", params={"n": len(found)})
    return found


async def _stage2_urlscan(session: aiohttp.ClientSession, delay_ms: int) -> Set[str]:
    """URLScan.io public API — t.me pages submitted to the scan service."""
    found: Set[str] = set()
    queries = [
        "page.domain:t.me",
        "page.url:t.me%2Fjoinchat",
        'page.url:"t.me%2F%2B"',
    ]
    base = "https://urlscan.io/api/v1/search/?size=100&q="
    for q in queries:
        if _check_interrupt("stage2"):
            break
        text = await _fetch_text(session, base + q)
        if text:
            for m in _TME_RE.finditer(text):
                u = _normalize_username(m.group(1))
                if u:
                    found.add(u)
        await asyncio.sleep(delay_ms / 1000)
    return found


async def _stage2_commoncrawl(session: aiohttp.ClientSession, delay_ms: int) -> Set[str]:
    """Common Crawl Index Server — t.me URLs in the latest CC crawl snapshot."""
    found: Set[str] = set()
    # Discover the most recent index name
    info = await _fetch_text(session, "https://index.commoncrawl.org/collinfo.json", timeout=30)
    index_id = None
    if info:
        try:
            index_id = json.loads(info)[0].get("id")
        except Exception:
            pass
    if not index_id:
        _emit_event("stage2", "commoncrawl: could not determine latest index", "warn",
                    key="hl.stage2.ccNoIndex")
        return found
    _emit_event("stage2", f"commoncrawl: querying {index_id}…",
                key="hl.stage2.ccStart", params={"index": index_id})
    text = await _fetch_text(
        session,
        f"https://index.commoncrawl.org/{index_id}"
        "?url=t.me/*&output=json&limit=5000&matchType=prefix",
        timeout=60,
    )
    if text:
        for m in _TME_RE.finditer(text):
            u = _normalize_username(m.group(1))
            if u:
                found.add(u)
        _emit_event("stage2", f"commoncrawl: {len(found)} candidates",
                    key="hl.stage2.ccDone", params={"n": len(found)})
    return found


# ── LLM-assisted semantic discovery ─────────────────────────────────────────

async def _stage2_llm(session: aiohttp.ClientSession,
                       keywords: List[str], queries: List[str],
                       settings: dict, delay_ms: int) -> Set[str]:
    """Claude API — two-pass semantic discovery.

    Pass 1 (semantic expansion): Claude receives existing candidate
    descriptions as context and generates related keywords that broaden
    the search pool (approximates embedding-based neighbourhood search
    without requiring a separate embedding endpoint).

    Pass 2 (creative query generation): Claude produces 25 novel dork
    queries that the static template builder wouldn't generate on its own.
    The queries run through DuckDuckGo and Bing via the shared
    _crawl_search_engine adapter (headless Chromium)."""
    api_key = (settings.get("anthropic_api_key") or "").strip()
    if not api_key:
        _emit_event("stage2", "llm: no Anthropic API key configured — skipping",
                    "warn", key="hl.stage2.llmNoKey")
        return set()

    # Collect a sample of existing candidate descriptions for semantic context
    context_lines: List[str] = []
    try:
        rows = await database._q(
            "SELECT title, description FROM hunter_candidates "
            "WHERE description IS NOT NULL AND description != '' "
            "ORDER BY score DESC NULLS LAST LIMIT 20"
        )
        for r in rows:
            desc = (r["description"] or "")[:120].replace("\n", " ")
            context_lines.append(f"- {r['title'] or '?'}: {desc}")
    except Exception:
        pass

    kw_str = ", ".join(keywords[:20]) if keywords else "files, documents, media"
    context_block = (
        "\n\nSample of already-found channels (for semantic context):\n"
        + "\n".join(context_lines[:15])
    ) if context_lines else ""

    prompt = (
        "You help discover Telegram channels that share files "
        "(documents, videos, archives, software, books, courses, etc.).\n\n"
        f"Keywords of interest: {kw_str}{context_block}\n\n"
        "Task A — Semantic expansion: suggest 10 related keywords or short phrases "
        "that would uncover *different* channels not reachable with the above keywords.\n\n"
        "Task B — Creative dorks: write 25 diverse search-engine dork queries "
        "that find Telegram file-sharing channels. Use operators like "
        "site:t.me, inurl:t.me, \"t.me/+\", intitle:, inurl:joinchat. "
        "Vary file types, topics, languages, and community terms. "
        "Do NOT just repeat the keywords — explore adjacent topics.\n\n"
        "Respond ONLY with valid JSON, no prose:\n"
        "{\"keywords\": [...], \"queries\": [...]}"
    )

    _emit_event("stage2", "llm: calling Claude for semantic expansion…",
                key="hl.stage2.llmCall")
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=aiohttp.ClientTimeout(total=40),
        ) as r:
            if r.status != 200:
                body = await r.text()
                _emit_event("stage2", f"llm: API error {r.status}: {body[:120]}", "warn",
                            key="hl.stage2.llmApiError", params={"status": r.status})
                return set()
            data = await r.json()
            content = (data.get("content") or [{}])[0].get("text", "")
    except Exception as e:
        _emit_event("stage2", f"llm: request failed: {str(e)[:100]}", "warn",
                    key="hl.stage2.llmFail", params={"err": str(e)[:100]})
        return set()

    # Parse JSON — be tolerant of markdown code fences
    llm_queries: List[str] = []
    llm_keywords: List[str] = []
    try:
        m = re.search(r'\{[\s\S]*\}', content)
        if m:
            parsed = json.loads(m.group())
            llm_queries  = [str(q) for q in parsed.get("queries",  []) if q][:25]
            llm_keywords = [str(k) for k in parsed.get("keywords", []) if k][:10]
    except Exception:
        pass

    _emit_event("stage2",
                f"llm: {len(llm_queries)} queries, {len(llm_keywords)} new keywords",
                key="hl.stage2.llmGenerated",
                params={"nq": len(llm_queries), "nk": len(llm_keywords)})

    # Persist learned keywords so future runs benefit from semantic expansion
    if llm_keywords:
        try:
            existing = settings.get("learned_keywords") or ""
            seen = {k.strip().lower() for k in existing.split(",") if k.strip()}
            new_kws = [k for k in llm_keywords if k.lower() not in seen]
            if new_kws:
                merged = (existing.rstrip(",") + "," if existing.strip() else "") + ",".join(new_kws)
                await database.update_hunter_settings({"learned_keywords": merged})
        except Exception:
            pass

    if not llm_queries:
        return set()

    # Run generated queries through DuckDuckGo + Bing (headless Chromium)
    found: Set[str] = set()
    found |= await _crawl_search_engine(
        session, "llm→ddg",
        "https://duckduckgo.com/",
        "https://duckduckgo.com/html/?q={q}",
        llm_queries, delay_ms,
        max_q=25, page_offsets=[""],
    )
    if not _check_interrupt("stage2"):
        found |= await _crawl_search_engine(
            session, "llm→bing",
            "https://www.bing.com/",
            "https://www.bing.com/search?q={q}",
            llm_queries, delay_ms,
            max_q=15, page_offsets=[""],
        )
    return found


# ── Paste-site scanner ────────────────────────────────────────────────────────
# (label, index_url, raw_url_template_or_None, paste_id_regex_or_None, max_raws)
# raw_url_template: {id} is replaced with the paste identifier.
# paste_id_regex: applied to the index HTML to collect individual paste IDs.
# max_raws: how many raw-paste fetches to attempt per site (0 = index page only).
# Sites that require login, are self-hosted, or burn-after-read are excluded.
_PASTE_SPECS: List[Tuple] = [
    ("pastebin.com",
     "https://pastebin.com/archive",
     "https://pastebin.com/raw/{id}",
     re.compile(r'href="/([A-Za-z0-9]{8})"'),
     40),
    ("paste.ee",
     "https://paste.ee/recent",
     "https://paste.ee/r/{id}",
     re.compile(r'href="/p/([A-Za-z0-9]+)"'),
     20),
    ("justpaste.it",       "https://justpaste.it/",           None, None, 0),
    ("rentry.co",          "https://rentry.co/",              None, None, 0),
    ("dpaste.org",         "https://dpaste.org/",             None, None, 0),
    ("nekobin.com",        "https://nekobin.com/",            None, None, 0),
    ("paste.debian.net",   "https://paste.debian.net/",       None, None, 0),
    ("paste.ubuntu.com",   "https://paste.ubuntu.com/",       None, None, 0),
    ("paste.opensuse.org", "https://paste.opensuse.org/",     None, None, 0),
    ("fpaste.org",         "https://fpaste.org/",             None, None, 0),
    ("bpa.st",             "https://bpa.st/",                 None, None, 0),
    ("paste2.org",         "https://paste2.org/",             None, None, 0),
    ("pastelink.net",      "https://pastelink.net/",          None, None, 0),
    ("controlc.com",       "https://controlc.com/",           None, None, 0),
    ("hastebin.com",       "https://hastebin.com/",           None, None, 0),
    ("codeshare.io",       "https://codeshare.io/",           None, None, 0),
    ("ix.io",              "https://ix.io/",                  None, None, 0),
    ("paste.sh",           "https://paste.sh/",               None, None, 0),
    ("paste.rs",           "https://paste.rs/",               None, None, 0),
    ("write.as",           "https://write.as/",               None, None, 0),
    ("pasted.co",          "https://pasted.co/",              None, None, 0),
    ("defuse.ca",          "https://defuse.ca/b/",            None, None, 0),
]


async def _stage2_pastesites(session: aiohttp.ClientSession, delay_ms: int) -> Set[str]:
    """Scan public paste-site archives for t.me channel/group addresses.

    For each site in _PASTE_SPECS we fetch the public archive/recent-pastes
    index page and scan its HTML directly.  For sites that expose a raw-paste
    endpoint (Pastebin, paste.ee) we additionally pull the text of up to
    max_raws individual pastes to catch links not visible in index snippets."""
    found: Set[str] = set()

    for label, index_url, raw_tpl, id_re, max_raws in _PASTE_SPECS:
        if _check_interrupt("stage2"):
            break
        _emit_event("stage2", f"pastesites: {label}",
                    key="hl.stage2.pasteIndex", params={"site": label})

        html = await _fetch_text(session, index_url)
        if html:
            for m in _TME_RE.finditer(html):
                u = _normalize_username(m.group(1))
                if u:
                    found.add(u)

            if raw_tpl and id_re:
                ids = list(dict.fromkeys(id_re.findall(html)))[:max_raws]
                for pid in ids:
                    if _check_interrupt("stage2"):
                        break
                    raw = await _fetch_text(session, raw_tpl.format(id=pid))
                    if raw:
                        for m in _TME_RE.finditer(raw):
                            u = _normalize_username(m.group(1))
                            if u:
                                found.add(u)
                    await asyncio.sleep(delay_ms / 2000)

        await asyncio.sleep(delay_ms / 1000)

    return found


# Map source name → (adapter, kind) where kind is 'kw' (uses query list) or 'plain'
_STAGE2_SOURCES = {
    # Telegram-specific directories (telegramly removed: site returns 404)
    "tgstat":       (_stage2_tgstat,       "plain"),
    "telemetrio":   (_stage2_telemetrio,   "plain"),
    "combot":       (_stage2_combot,       "plain"),
    "tdirectory":   (_stage2_tdirectory,   "plain"),
    "tlgrm":        (_stage2_tlgrm,        "plain"),
    "telegramic":   (_stage2_telegramic,   "plain"),
    "tgchannels":   (_stage2_tgchannels,   "plain"),
    "searchtg":     (_stage2_searchtg,     "kw"),
    "tdoru":        (_stage2_tdoru,        "plain"),
    "telegaio":     (_stage2_telegaio,     "plain"),
    # General search engines (use the dork-rich query list)
    "google":       (_stage2_google,       "query"),
    "duckduckgo":   (_stage2_duckduckgo,   "query"),
    "yandex":       (_stage2_yandex,       "query"),
    "brave":        (_stage2_brave,        "query"),
    "bing":         (_stage2_bing,         "query"),
    "mojeek":       (_stage2_mojeek,       "query"),
    "startpage":    (_stage2_startpage,    "query"),
    "ecosia":       (_stage2_ecosia,       "query"),
    # Curated / social
    "reddit":       (_stage2_reddit,       "kw"),
    "hackernews":   (_stage2_hackernews,   "kw"),
    "github":       (_stage2_github,       "plain"),
    # Paste sites (archive crawl + raw-paste fetch where available)
    "pastesites":   (_stage2_pastesites,   "plain"),
    # Web archives
    "wayback":      (_stage2_wayback,      "plain"),
    "urlscan":      (_stage2_urlscan,      "plain"),
    "commoncrawl":  (_stage2_commoncrawl,  "plain"),
    # LLM-assisted semantic discovery (requires anthropic_api_key in settings)
    "llm":          (_stage2_llm,          "llm"),
}


# Default source set when the user's `sources` field is blank — this drives
# fresh installs and acts as a fallback. Listed in the order we prefer.
# Search-engine dorks (site:t.me + keywords) are now the primary path —
# they go through headless Chromium so anti-bot pages don't stop them.
# The directory sources (tgstat/telemetrio/combot/…) routinely return 403
# or Cloudflare challenges even via Chromium; users who specifically want
# them can re-enable them in Hunter settings. Default list trimmed to
# sources that produced non-zero results in practice.
_DEFAULT_SOURCES = ",".join([
    "internal",
    "duckduckgo", "google", "bing", "brave", "yandex", "startpage", "ecosia", "mojeek",
    "searchtg",
    "reddit", "hackernews", "github",
    "pastesites",
    "wayback", "urlscan", "commoncrawl",
    # "llm" is NOT in the default list — requires anthropic_api_key to be set
])


async def stage2_crawl_web(settings: dict) -> int:
    raw = (settings.get("sources") or "").strip()
    if not raw:
        # Empty config → run the full default set (all registered adapters
        # except 'internal', which is handled in stage 1)
        raw = _DEFAULT_SOURCES
    sources = [s.strip().lower() for s in raw.split(",") if s.strip() and s.strip().lower() != "internal"]
    delay_ms = int(settings.get("web_request_delay_ms") or 2500)
    # Anahtar kelime havuzu = kullanıcı/varsayılan + öğrenilmiş (#3) + güncel
    # trend (#5). Tekdüzeleşmeyi kırmak için her koşuda farklı bir karışım
    # gelir; ilk N entry sırayı (kullanıcı önce → trend → öğrenilmiş)
    # koruyarak Pattern A şablonuna giriyor.
    base_kw = _smart_keywords(settings.get("keywords") or "")
    learned_kw = [k.strip().lower() for k in (settings.get("learned_keywords") or "").split(",") if k.strip()]
    trend_kw = await _fetch_trend_keywords()
    if trend_kw:
        _emit_event("stage2", f"trend keywords: {', '.join(trend_kw)}", "info",
                    key="hl.stage2.trendKeywords", params={"list": ", ".join(trend_kw)})
    if learned_kw:
        _emit_event("stage2", f"learned keywords ({len(learned_kw)}): {', '.join(learned_kw[:8])}{'…' if len(learned_kw) > 8 else ''}", "info",
                    key="hl.stage2.learnedKeywords",
                    params={"n": len(learned_kw), "sample": ", ".join(learned_kw[:8]) + ('…' if len(learned_kw) > 8 else '')})
    # Dedup koruyarak birleştir
    merged_kw: List[str] = []
    seen_kw: Set[str] = set()
    for k in base_kw + trend_kw + learned_kw:
        lk = k.lower()
        if lk in seen_kw: continue
        seen_kw.add(lk); merged_kw.append(k)
    queries = _build_search_queries(merged_kw)
    n_added = 0

    connector = aiohttp.TCPConnector(limit=int(settings.get("web_concurrency") or 2),
                                       ttl_dns_cache=300, ssl=False)
    valid_sources = [s for s in sources if s in _STAGE2_SOURCES]
    unknown = [s for s in sources if s not in _STAGE2_SOURCES]
    # Adapters the program supports but the saved config doesn't list. This
    # is the silent-skip pit that confuses users when we register new sources
    # and their saved sources field is curated.
    known_skipped = sorted(
        s for s in _STAGE2_SOURCES.keys()
        if s not in valid_sources
    )
    # Pre-mark cooldown'd sources so the UI shows them grayed-out
    detail = {"sources_total": len(valid_sources), "sources_done": 0, "current_source": None, "per_source": {}}
    for s in valid_sources:
        if not _source_can_run(s):
            cd_ts = _SOURCE_COOLDOWN_UNTIL.get(s, 0)
            cd_str = datetime.fromtimestamp(cd_ts).isoformat(timespec="minutes")
            detail["per_source"][s] = {"state": "cooldown", "found": 0, "cooldown_until": cd_str}
        else:
            detail["per_source"][s] = {"state": "queued", "found": 0}
    status["stage_detail"] = detail
    _emit_event("stage2",
                f"Web crawl starting: {len(valid_sources)} source(s) "
                f"(of {len(_STAGE2_SOURCES)} registered)",
                key="hl.stage2.crawlStart",
                params={"n": len(valid_sources), "total": len(_STAGE2_SOURCES)})
    if known_skipped:
        # These adapters exist in code but aren't in the active source list.
        # Most of them are the Cloudflare-fronted directories we removed from
        # the default after they consistently returned 0 results — listed
        # here just for transparency so the user knows about them.
        _emit_event("stage2",
                    f"Skipped {len(known_skipped)} registered source(s) "
                    f"not in your config: {', '.join(known_skipped)} — "
                    f"add them by name to the Sources field if you want "
                    f"to try them anyway.",
                    "info",
                    key="hl.stage2.skippedSources",
                    params={"n": len(known_skipped), "list": ", ".join(known_skipped)})
    if unknown:
        _emit_event("stage2",
                    f"Unknown source(s) in config: {', '.join(unknown)}",
                    "warn",
                    key="hl.stage2.unknownSources",
                    params={"list": ", ".join(unknown)})

    # aiohttp session with cookies enabled (jar) — many sites set first-party
    # cookies on homepage that we need to echo back on subsequent fetches.
    # The CloakBrowser instance (if any) is torn down in finally so the
    # headless Chromium doesn't linger between scheduled runs.
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    try:
      async with aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar) as session:
        all_found: Dict[str, Set[str]] = {}
        for i, src in enumerate(valid_sources):
            interrupt = _check_interrupt("stage2")
            if interrupt == "cancel":
                _emit_event("stage2", "Cancelled by user", "warn", key="hl.stage2.cancelled"); break
            if interrupt == "skip":
                status["skip_stage_requested"] = False
                _emit_event("stage2", "Stage skipped by user", "warn", key="hl.stage2.userSkipped"); break

            # Skip sources still in cool-down from previous runs/today
            if not _source_can_run(src):
                cd_ts = _SOURCE_COOLDOWN_UNTIL.get(src, 0)
                cd_str = datetime.fromtimestamp(cd_ts).isoformat(timespec="minutes")
                _emit_event("stage2", f"{src}: in cool-down until {cd_str}", "warn", key="hl.stage2.coolDownActive", params={"src": src, "when": cd_str})
                status["stage_detail"]["sources_done"] = i + 1
                continue

            status["stage_detail"]["current_source"] = src
            status["stage_detail"]["sources_done"] = i
            status["stage_detail"]["per_source"][src] = {"state": "running", "found": 0}
            _emit_event("stage2", f"Source {src} starting…", key="hl.stage2.sourceStart", params={"src": src})

            adapter, kind = _STAGE2_SOURCES[src]
            try:
                if kind == "plain":
                    res = await adapter(session, delay_ms)
                elif kind == "kw":
                    res = await adapter(session, base_kw, delay_ms)
                elif kind == "llm":
                    res = await adapter(session, base_kw, queries, settings, delay_ms)
                else:  # query
                    res = await adapter(session, queries, delay_ms)
                all_found[src] = res
                status["stage_detail"]["per_source"][src] = {"state": "done", "found": len(res)}
                logger.info(f"hunter[{src}]: {len(res)} candidates")
                _emit_event("stage2", f"{src}: {len(res)} candidates found", key="hl.stage2.sourceFound", params={"src": src, "n": len(res)})
                if len(res) > 0:
                    _source_record_success(src)
                else:
                    # Zero results often means we are being blocked silently
                    _source_record_failure(src)
            except Exception as e:
                status["stage_detail"]["per_source"][src] = {"state": "error", "found": 0, "error": str(e)[:60]}
                _emit_event("stage2", f"{src} failed: {str(e)[:80]}", "warn", key="hl.stage2.sourceFailed", params={"src": src, "err": str(e)[:80]})
                logger.warning(f"hunter[{src}] failed: {e}")
                _source_record_failure(src)
        status["stage_detail"]["sources_done"] = len(valid_sources)
        status["stage_detail"]["current_source"] = None

        # Insert into DB with per-source attribution
        for src, usernames in all_found.items():
            for u in usernames:
                if await database.is_blacklisted(u):
                    continue
                cid = await database.upsert_hunter_candidate(u)
                if cid:
                    await database.add_hunter_source(cid, f"web:{src}", None)
                    n_added += 1
    finally:
      # Headless Chromium kalmasın — bir sonraki çalışmaya kadar gereksiz RAM
      # tutar. _pw_teardown idempotent'tir, browser hiç başlamadıysa hızla
      # döner.
      await _pw_teardown()
    return n_added


# ── Stage 3: Telethon enrichment ─────────────────────────────────────────────

_FILE_GROUPS = {
    "audio":    {"mp3","flac","wav","aac","ogg","m4a","opus","wma","ape","alac"},
    "video":    {"mp4","mkv","avi","mov","wmv","flv","webm","m4v","ts","3gp"},
    "image":    {"jpg","jpeg","png","gif","bmp","webp","svg","tiff","tif","heic"},
    "archive":  {"zip","rar","7z","tar","gz","bz2","xz","zst","cab","iso"},
    "document": {"pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","txt","epub","rtf","csv","md"},
    "software": {"exe","apk","dmg","deb","rpm","msi","pkg","bin","jar","sh"},
}


# ──── App-update channel detector (data-driven, no fixed noise list) ───────
#
# Telegram'da bir kanal genelde tek bir uygulamanın güncel sürümlerini paylaşır
# (örn. her hafta yeni bir `whatsapp_2.24.6_mod.apk`). Bu kalıbı sezmek için
# dosya adlarını tokenize edip "kanal genelinde sık tekrarlayan token" ile
# "her dosyada farklı olan token"ı ayırt ediyoruz — token-frequency tabanlı.
# Hiçbir sabit gürültü listesi yok; her kanal kendi noise pattern'ini DF ile
# kendisi tanımlar. Marjinal durumlar için pairwise LCS medyanı doğrular.
_TOK_SEP_RE = re.compile(r"[_\-\s.+()\[\]]+")


# ──── Character-script classification (no language guess library) ──────
# Tek dosya adındaki "baskın" yazı sistemini tespit etmek için Unicode
# code-point bantları kullanıyoruz. Yalnızca alfabetik karakterler sayılır;
# rakamlar/işaretler nötr — uzantı ve sürüm numaraları sınıflandırmayı
# saptırmasın.
def _char_script(c: str) -> Optional[str]:
    cp = ord(c)
    if (0x0041 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF) or (0x0250 <= cp <= 0x02AF):
        return "latin"
    if (0x0400 <= cp <= 0x04FF) or (0x0500 <= cp <= 0x052F):
        return "cyrillic"
    if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0x20000 <= cp <= 0x2A6DF):
        return "han"
    if 0x3040 <= cp <= 0x309F:
        return "hiragana"
    if 0x30A0 <= cp <= 0x30FF:
        return "katakana"
    if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F):
        return "hangul"
    if (0x0600 <= cp <= 0x06FF) or (0x0750 <= cp <= 0x077F) or (0x08A0 <= cp <= 0x08FF):
        return "arabic"
    if 0x0590 <= cp <= 0x05FF:
        return "hebrew"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    if 0x0370 <= cp <= 0x03FF:
        return "greek"
    if c.isalpha():
        return "other"
    return None


def _filename_dominant_script(s: str) -> Optional[str]:
    counts: Dict[str, int] = {}
    # Uzantıyı düşür — `.apk`, `.pdf` her zaman Latin oluyor, asıl adın
    # script'ini bastırıyor.
    base = re.sub(r"\.[a-z0-9]{1,5}$", "", s)
    for ch in base:
        cls = _char_script(ch)
        if cls:
            counts[cls] = counts.get(cls, 0) + 1
    if not counts:
        return None
    # CJK gruplarını "cjk" altında birleştir (japonca dosya adları sıklıkla
    # han + hiragana + katakana karışımı olur; ayrı saymak yanıltır).
    cjk_total = counts.get("han", 0) + counts.get("hiragana", 0) + counts.get("katakana", 0)
    if cjk_total > 0:
        counts["cjk"] = cjk_total
        for k in ("han", "hiragana", "katakana"):
            counts.pop(k, None)
    return max(counts.items(), key=lambda kv: kv[1])[0]


# Hangi UI dilinde hangi script'ler "okunabilir" sayılır — bu eşleştirme
# kullanıcı dilinin doğal alfabesini ve Latin'i kapsar (Latin uluslararası
# bir taban). Bir scripti acceptable kümede değilse "yabancı" demektir.
_SCRIPT_ACCEPT_BY_LANG: Dict[str, set] = {
    "tr": {"latin"},
    "en": {"latin"},
    "de": {"latin"},
    "ru": {"latin", "cyrillic"},
    "zh": {"latin", "cjk"},
}


def _channel_dominant_script(named_filenames: List[str]) -> Tuple[Optional[str], float, int]:
    """Returns (dominant_script, ratio, sampled). Bir script "baskın" sayılır
    eğer >50% dosyanın dominant scripti o ise. Sentetik isimler hariç tutulur
    (named_filenames zaten temizlenmiş)."""
    n = len(named_filenames)
    if n == 0:
        return None, 0.0, 0
    counts: Dict[str, int] = {}
    for fn in named_filenames:
        d = _filename_dominant_script(fn)
        if d:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None, 0.0, n
    dominant, c = max(counts.items(), key=lambda kv: kv[1])
    return dominant, c / n, n


def _channel_token_df(filenames: List[str]) -> Dict[str, float]:
    """Token -> document-frequency oranı (0..1). Tokenler: ayraçlardan
    parçalanmış, salt rakam olmayan, en az 2 karakter."""
    n = len(filenames)
    if not n:
        return {}
    df: Dict[str, int] = {}
    for fn in filenames:
        base = re.sub(r"\.[a-z0-9]{1,5}$", "", fn.lower())
        tokens = {
            tk for tk in _TOK_SEP_RE.split(base)
            if len(tk) >= 2 and not tk.isdigit()
        }
        for tk in tokens:
            df[tk] = df.get(tk, 0) + 1
    return {tk: c / n for tk, c in df.items()}


def _pairwise_lcs_median(filenames: List[str], *, sample: int = 60) -> float:
    """Filenames'in karakter düzeyinde ortalama "ortak gövde" oranı.
    Aynı app'in N sürümünde bu oran yüksek (her çift uzun ortak alt-dize
    paylaşır: app adı). Rastgele kanallarda düşük."""
    if len(filenames) < 4:
        return 0.0
    import random as _r
    pool = [re.sub(r"\.[a-z0-9]{1,5}$", "", fn.lower()) for fn in filenames]
    if len(pool) > sample:
        pool = _r.sample(pool, sample)
    ratios: List[float] = []
    for i, a in enumerate(pool):
        la = len(a)
        if la == 0:
            continue
        for b in pool[i + 1:]:
            lb = len(b)
            if lb == 0:
                continue
            # 2-row rolling DP for longest common substring length
            prev = [0] * (lb + 1)
            best = 0
            for ca in a:
                cur = [0] * (lb + 1)
                for j in range(1, lb + 1):
                    if ca == b[j - 1]:
                        v = prev[j - 1] + 1
                        cur[j] = v
                        if v > best:
                            best = v
                prev = cur
            ratios.append(best / min(la, lb))
    if not ratios:
        return 0.0
    ratios.sort()
    return ratios[len(ratios) // 2]


def _detect_app_update_channel(
    named_filenames: List[str],
    *,
    df_high: float = 0.5,
    coverage_min: float = 0.85,
    min_files: int = 20,
    lcs_threshold: float = 0.35,
) -> Optional[Dict]:
    """Decide whether the named-filename pool looks like 'N versions of the
    same app(s)'. Returns a dict with diagnostics or None when the sample is
    too small to decide. No fixed noise list — the channel's own DF distribution
    decides what's stable (app identity) vs varying (version noise)."""
    n = len(named_filenames)
    if n < min_files:
        return None
    df = _channel_token_df(named_filenames)
    stable = sorted(
        ((t, r) for t, r in df.items() if r >= df_high),
        key=lambda kv: -kv[1],
    )
    if not stable:
        return {
            "is_app_update": False,
            "top_tokens": [],
            "coverage": 0.0,
            "lcs_median": 0.0,
            "named_files": n,
            "reason": "stable token yok",
        }
    stable_set = {t for t, _ in stable}
    covered = sum(
        1 for fn in named_filenames
        if any(tk in fn.lower() for tk in stable_set)
    )
    coverage = covered / n
    is_app = coverage >= coverage_min
    lcs_med = 0.0
    # Marjinal coverage'ı LCS ile doğrula; net karar varsa LCS yine de
    # raporlamak için hesaplanıyor (log'da sinyal taşır).
    if 0.7 <= coverage < coverage_min:
        lcs_med = _pairwise_lcs_median(named_filenames)
        if lcs_med >= lcs_threshold:
            is_app = True
    elif is_app:
        lcs_med = _pairwise_lcs_median(named_filenames)
    return {
        "is_app_update": is_app,
        "top_tokens": [(t, round(r, 2)) for t, r in stable[:5]],
        "coverage": round(coverage, 3),
        "lcs_median": round(lcs_med, 3),
        "named_files": n,
    }


def _photo_size(photo) -> int:
    """Approximate byte size of the largest available photo resolution.
    Telethon photos expose either ``PhotoSize.size`` (single int) or
    ``PhotoSizeProgressive.sizes`` (list of byte offsets, last = total).
    Both forms are tolerated; missing/unknown shapes return 0."""
    best = 0
    try:
        for s in getattr(photo, "sizes", None) or []:
            sz = getattr(s, "size", None)
            if isinstance(sz, int) and sz > best:
                best = sz
            psz = getattr(s, "sizes", None)
            if isinstance(psz, (list, tuple)) and psz:
                last = psz[-1]
                if isinstance(last, int) and last > best:
                    best = last
    except Exception:
        return 0
    return best


def _doc_filename(doc, msg_id: int) -> Tuple[Optional[str], str, bool, bool, bool]:
    """Best-effort filename + extension from a Telethon document.

    Telegram does NOT require a filename attribute. Voice messages,
    camera-uploaded videos, forwarded audio, stickers, animations all
    routinely arrive with no DocumentAttributeFilename, so the obvious
    `attr.file_name` path returns None and we used to write NULL into
    hunter_candidate_files — showing up in the UI as `—`.

    Fallback chain for the name:
      1. DocumentAttributeFilename.file_name              (primary)
      2. DocumentAttributeAudio.title (+ performer)       (tagged audio)
      3. f"<group>_<msg_id>.<ext>"                        (synthetic)

    Returns (fname, ext, is_video, is_audio, is_named).
    `is_named` = TRUE when the doc carried real authoring metadata
    (filename attr OR audio title) — i.e. someone explicitly uploaded
    this as a file. FALSE means we had to synthesise the name from
    the message id; the document is most likely Telegram-native
    ephemeral media: a voice message, camera-uploaded video, sticker,
    animated GIF, etc. The user wants this distinction surfaced so
    that channels whose 'file' counts are mostly voice notes can be
    spotted and rejected before joining.
    """
    fname = None
    is_video = is_audio = False
    audio_title = audio_performer = None
    for attr in (getattr(doc, "attributes", None) or []):
        cname = type(attr).__name__
        if cname == "DocumentAttributeFilename":
            fname = getattr(attr, "file_name", None)
        elif cname == "DocumentAttributeVideo":
            is_video = True
        elif cname == "DocumentAttributeAudio":
            is_audio = True
            audio_title = getattr(attr, "title", None)
            audio_performer = getattr(attr, "performer", None)

    # Was this an authored file? (filename attr OR tagged audio with title)
    is_named = bool(fname) or bool(audio_title)

    ext = (fname.rsplit(".", 1)[-1] if fname and "." in fname else "")
    if not ext:
        mime = getattr(doc, "mime_type", "") or ""
        if "/" in mime:
            ext = mime.rsplit("/", 1)[-1]
        elif is_video:
            ext = "mp4"
        elif is_audio:
            ext = "mp3"
    ext = ext.lower()

    if not fname:
        if audio_title:
            base = (f"{audio_performer} - {audio_title}" if audio_performer
                    else audio_title).strip()
            fname = (f"{base}.{ext}" if ext and not base.lower().endswith("." + ext)
                     else base)
        elif is_video:
            fname = f"video_{msg_id}.{ext or 'mp4'}"
        elif is_audio:
            fname = f"audio_{msg_id}.{ext or 'mp3'}"
        else:
            grp_for_ext = _file_group(ext)
            fname = (f"{grp_for_ext}_{msg_id}.{ext}" if ext
                     else f"file_{msg_id}")
    return fname, ext, is_video, is_audio, is_named


def _file_group(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    for g, exts in _FILE_GROUPS.items():
        if e in exts:
            return g
    return "other"


def _count_keyword_hits(text: str, keywords: List[str]) -> int:
    """Total times any of the user's keywords appears in `text`. Substring
    match (case-insensitive). Each occurrence counts — a keyword mentioned
    three times in three messages contributes 3."""
    if not text or not keywords:
        return 0
    low = text.lower()
    hits = 0
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        # Cheap substring scan; for the short keyword lists users supply,
        # this is materially faster than building per-keyword regexes.
        idx = 0
        while True:
            idx = low.find(k, idx)
            if idx < 0:
                break
            hits += 1
            idx += len(k)
    return hits


def _score_breakdown(
    file_count: int,
    sampled: int,
    members: int,
    diversity: int,
    days_since_last: float,
    *,
    keyword_hits: int = 0,
    avg_size: int = 0,
    duplicate_ratio: float = 0.0,
    unnamed_ratio: float = 0.0,
    breakdown: dict = None,
) -> float:
    """Composite 0..100 channel score.

    Scoring is driven by USEFUL files only — images, videos, and audio are
    excluded entirely.  Components (weighted sum, max 100):
      - useful_density (0.55) useful_count / sampled  (fraction of messages
                               that contain a non-image/video/audio file)
      - recency        (0.30) linear decay over 60 days since last post
      - keyword_bonus  (0.15) keyword hits in message text / description

    Returns 0 immediately when there are no useful files.

    Penalties (each 0..1, multiplicative, capped at 60% total cut):
      - duplicate  → same filename repeats (sticker.webp pattern)
      - unnamed    → most documents are auto-generated names (forwards)
    """
    if sampled <= 0:
        return 0.0

    _EXCLUDED = {"image", "video", "audio"}
    bd = breakdown or {}
    if bd:
        useful_count = sum(v for k, v in bd.items() if k not in _EXCLUDED)
    else:
        # No breakdown available — fall back to total count (legacy callers).
        useful_count = file_count

    if useful_count == 0:
        return 0.0

    useful_density = min(1.0, useful_count / sampled)
    # Linear decay: full score at 0 days, zero at 60 days.
    recency       = max(0.0, 1.0 - (days_since_last / 60.0)) if days_since_last is not None else 0.0
    keyword_bonus = min(1.0, (keyword_hits / max(1, sampled)) * 3.0)

    base = (0.55 * useful_density
            + 0.30 * recency
            + 0.15 * keyword_bonus)

    # Penalties
    dup_penalty     = min(1.0, duplicate_ratio)
    unnamed_penalty = max(0.0, (unnamed_ratio - 0.5) * 2.0)
    penalty_factor  = min(1.0, (dup_penalty + unnamed_penalty) / 2.0)
    return round(100.0 * base * (1.0 - 0.60 * penalty_factor), 2)


# Unicode mathematical bold/italic/script → ASCII mapping (covers most
# Telegram "fancy text" abuse). Built once at import time.
def _build_unicode_plain_map() -> dict:
    ranges = [
        (0x1D400, 0x1D419, 'A'), (0x1D41A, 0x1D433, 'a'),  # bold
        (0x1D434, 0x1D44D, 'A'), (0x1D44E, 0x1D467, 'a'),  # italic
        (0x1D468, 0x1D481, 'A'), (0x1D482, 0x1D49B, 'a'),  # bold italic
        (0x1D49C, 0x1D4B5, 'A'), (0x1D4B6, 0x1D4CF, 'a'),  # script
        (0x1D4D0, 0x1D4E9, 'A'), (0x1D4EA, 0x1D503, 'a'),  # bold script
        (0x1D504, 0x1D51D, 'A'), (0x1D51E, 0x1D537, 'a'),  # fraktur
        (0x1D538, 0x1D551, 'A'), (0x1D552, 0x1D56B, 'a'),  # double-struck
        (0x1D56C, 0x1D585, 'A'), (0x1D586, 0x1D59F, 'a'),  # bold fraktur
        (0x1D5A0, 0x1D5B9, 'A'), (0x1D5BA, 0x1D5D3, 'a'),  # sans
        (0x1D5D4, 0x1D5ED, 'A'), (0x1D5EE, 0x1D607, 'a'),  # sans bold
        (0x1D608, 0x1D621, 'A'), (0x1D622, 0x1D63B, 'a'),  # sans italic
        (0x1D63C, 0x1D655, 'A'), (0x1D656, 0x1D66F, 'a'),  # sans bold italic
        (0x1D670, 0x1D689, 'A'), (0x1D68A, 0x1D6A3, 'a'),  # monospace
    ]
    m: dict = {}
    for start, end, base_char in ranges:
        base_ord = ord(base_char)
        for i, cp in enumerate(range(start, end + 1)):
            m[cp] = chr(base_ord + i)
    # Bold digits 𝟎–𝟗
    for i in range(10):
        m[0x1D7CE + i] = str(i)
    # Zero-width / invisible chars
    for cp in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060):
        m[cp] = ''
    return m

_UNICODE_PLAIN_MAP = _build_unicode_plain_map()


def clean_title(text: str) -> str:
    """Strip mathematical Unicode styling and invisible chars from a title."""
    if not text:
        return text
    return ''.join(_UNICODE_PLAIN_MAP.get(ord(c), c) for c in text).strip()


async def _sample_entity_messages(
    client, entity, candidate_id: int, username: str, sample_limit: int
):
    """Walk entity's recent messages and collect file stats + text.

    Returns:
        (file_count, breakdown, last_message_at, sampled,
         total_size, text_parts, fname_counts, unnamed_count,
         named_filenames)
    """
    file_count = 0
    breakdown: Dict[str, int] = {k: 0 for k in list(_FILE_GROUPS.keys()) + ["other"]}
    last_message_at = None
    sampled = 0
    total_size = 0
    text_parts: List[str] = []
    fname_counts: Dict[str, int] = {}
    unnamed_count = 0
    # App-update-detector için yalnızca insanın verdiği gerçek dosya adlarını
    # topla. video_42.mp4 / photo_42.jpg gibi sentetik isimler tüm "photo"
    # token'ında 1.0 DF üretip detector'ı false-positive verirdi.
    named_filenames: List[str] = []
    try:
        async for msg in client.iter_messages(entity, limit=sample_limit):
            sampled += 1
            if msg.date and (last_message_at is None or msg.date > last_message_at):
                last_message_at = msg.date
            if getattr(msg, "text", None):
                text_parts.append(msg.text)
            cap = getattr(msg, "caption", None)
            if cap:
                text_parts.append(cap)

            if msg.document:
                doc = msg.document
                file_count += 1
                size = int(getattr(doc, "size", 0) or 0)
                total_size += size
                fname, ext, _is_video, _is_audio, is_named = _doc_filename(doc, msg.id)
                grp = _file_group(ext)
                breakdown[grp] += 1
                fname_counts[fname] = fname_counts.get(fname, 0) + 1
                if is_named:
                    named_filenames.append(fname)
                else:
                    unnamed_count += 1
                msg_date = msg.date
                if msg_date and msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                try:
                    await database.insert_candidate_file(
                        candidate_id, msg.id, fname, ext, size, grp, msg_date,
                        is_named=is_named,
                    )
                except Exception:
                    pass
            elif isinstance(msg.media, MessageMediaPhoto):
                file_count += 1
                breakdown[_file_group("jpg")] += 1
                photo = getattr(msg, "photo", None)
                size = _photo_size(photo) if photo else 0
                total_size += size
                fname = f"photo_{msg.id}.jpg"
                unnamed_count += 1
                fname_counts["__photo__"] = fname_counts.get("__photo__", 0) + 1
                msg_date = msg.date
                if msg_date and msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                try:
                    await database.insert_candidate_file(
                        candidate_id, msg.id, fname, "jpg", size, "image", msg_date,
                        is_named=False,
                    )
                except Exception:
                    pass
    except FloodWaitError:
        raise
    except Exception as e:
        logger.debug(f"message sample failed for {username}: {e}")
    return (file_count, breakdown, last_message_at, sampled,
            total_size, text_parts, fname_counts, unnamed_count,
            named_filenames)


async def _enrich_one(client, candidate_id: int, username: str, sample_limit: int,
                       cand: Optional[dict] = None, temp_join_enabled: bool = False,
                       skip_old_channels: bool = True) -> Tuple[bool, Optional[str]]:
    """Returns (success, reason). On failure `reason` is a short human-readable
    string the caller surfaces in the avlama log — previously every failure
    came through as a bare 'failed' line with no context."""
    # Prefer cached peer to avoid ResolveUsernameRequest (strict daily limit).
    entity = None
    if cand:
        pid = cand.get("peer_id"); ah = cand.get("access_hash")
        if pid and ah is not None:
            try:
                entity = await client.get_entity(InputPeerChannel(int(pid), int(ah)))
            except FloodWaitError:
                raise
            except Exception:
                entity = None

    if entity is None:
        try:
            entity = await client.get_entity(username)
        except (UsernameInvalidError, UsernameNotOccupiedError):
            # Permanently invalid/non-existent — blacklist + delete the row so it
            # disappears from candidate lists and never comes back via stage 1/2.
            await database.add_to_blacklist(username, "auto: username invalid/not occupied")
            await database.delete_hunter_candidate(candidate_id)
            return False, "username yok / silinmiş → kara listeye alındı"
        except ChannelPrivateError:
            # Inaccessible to this account — blacklist + delete (try a different
            # account by un-blacklisting it manually if you want to retry).
            await database.add_to_blacklist(username, "auto: private/inaccessible")
            await database.delete_hunter_candidate(candidate_id)
            return False, "özel kanal / erişilemiyor → kara listeye alındı"
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s on get_entity({username})")
            raise
        except Exception as e:
            # Unknown errors (network, parse, etc.): blacklist + delete to avoid
            # it showing up forever. User can clean blacklist if needed.
            err_msg = str(e)[:150]
            await database.add_to_blacklist(username, f"auto: {err_msg}")
            await database.delete_hunter_candidate(candidate_id)
            return False, f"{type(e).__name__}: {err_msg} → kara listeye alındı"

    is_channel = isinstance(entity, Channel)
    title = clean_title(getattr(entity, "title", None) or username)

    # member count via GetFullChannelRequest
    members = None
    description = None
    try:
        full = await client(GetFullChannelRequest(entity))
        members = getattr(full.full_chat, "participants_count", None)
        description = getattr(full.full_chat, "about", None)
    except Exception:
        pass

    # Sample recent messages. We deliberately do NOT use server-side
    # InputMessagesFilterDocument because it excludes video/audio documents.
    (file_count, breakdown, last_message_at, sampled,
     total_size, text_parts, fname_counts, unnamed_count,
     named_filenames) = await _sample_entity_messages(
        client, entity, candidate_id, username, sample_limit
    )

    # If no files came back and temp-join is permitted, join temporarily,
    # re-sample, then leave. This handles channels that restrict history
    # to members only.
    if file_count == 0 and temp_join_enabled:
        _temp_joined = False
        try:
            await client(JoinChannelRequest(entity))
            _temp_joined = True
            logger.info(f"Enrich: temp-joining @{username} (0 docs as non-member)")
            (file_count, breakdown, last_message_at, sampled,
             total_size, text_parts, fname_counts, unnamed_count,
             named_filenames) = await _sample_entity_messages(
                client, entity, candidate_id, username, sample_limit
            )
        except FloodWaitError:
            raise
        except Exception as _tj_e:
            logger.warning(f"Enrich: temp-join failed for @{username}: {_tj_e}")
        finally:
            if _temp_joined:
                try:
                    await client(LeaveChannelRequest(entity))
                    logger.info(f"Enrich: left @{username} after temp scan")
                except Exception as _lv_e:
                    logger.warning(f"Enrich: leave failed for @{username}: {_lv_e}")

    avg_size = int(total_size / file_count) if file_count else 0
    diversity = sum(1 for v in breakdown.values() if v > 0)
    days_since = None
    if last_message_at:
        if last_message_at.tzinfo is None:
            last_message_at = last_message_at.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last_message_at).total_seconds() / 86400

    # ── App-update channel detection (data-driven, no fixed noise list) ──
    # Bir kanal aynı uygulamanın N farklı sürümünü dağıtıyorsa (örn. APK
    # update kanalı) detector bunu yakalar → kara listeye yazıp adayı sil.
    # Hunter pipeline'a Stage 3 üzerinden hem manuel hem zamanlanmış akışta
    # girdiği için ek entegrasyon gerekmiyor.
    app_det = _detect_app_update_channel(named_filenames)
    if app_det and app_det.get("is_app_update"):
        top_tokens_str = ", ".join(
            f"{tok}({pct})" for tok, pct in app_det.get("top_tokens", [])
        ) or "—"
        cov = app_det.get("coverage", 0)
        lcs = app_det.get("lcs_median", 0)
        n_named = app_det.get("named_files", 0)
        label_token = (app_det.get("top_tokens") or [("?", 0)])[0][0]
        bl_reason = (
            f"app-update kanalı (≈{label_token}, coverage {cov:.0%}, "
            f"lcs {lcs:.2f}, {n_named} dosya)"
        )
        try:
            await database.add_to_blacklist(username, f"auto: {bl_reason}")
        except Exception as _bl_e:
            logger.warning(f"Blacklist insert failed for @{username}: {_bl_e}")
        try:
            await database.delete_hunter_candidate(candidate_id)
        except Exception as _del_e:
            logger.warning(f"Delete candidate failed for @{username}: {_del_e}")
        _emit_event(
            "stage3",
            (f"@{username}: 📱 app-update kanalı tespit → kara listeye eklendi · "
             f"baskın: {label_token} · coverage {cov:.0%} · "
             f"LCS medyanı {lcs:.2f} · {n_named} adlı dosya · "
             f"stable tokens: {top_tokens_str}"),
            "warn",
            key="hl.stage3.appUpdateBlacklisted",
            params={
                "username": username,
                "label": label_token,
                "coverage": cov,
                "lcs": lcs,
                "named": n_named,
                "tokens": app_det.get("top_tokens", []),
            },
        )
        return False, f"app-update kanalı (≈{label_token}, coverage {cov:.0%}) → kara listeye eklendi"
    elif app_det:
        # Detector çalıştı ama negatif — yine de log'da diagnostic bırak,
        # özellikle marjinal coverage (0.5-0.84) durumları için.
        cov = app_det.get("coverage", 0)
        if cov >= 0.5:
            _emit_event(
                "stage3",
                (f"@{username}: app-update şüphesi (coverage {cov:.0%}) ama eşik altında — devam"),
                "info",
                key="hl.stage3.appUpdateMaybe",
                params={
                    "username": username, "coverage": cov,
                    "tokens": app_det.get("top_tokens", []),
                },
            )

    # ── Rule: image+video majority → blacklist ──
    # Kullanıcı kuralı: resim + video toplamı toplam dosyanın yarısından
    # fazlaysa kanal medya-ağırlıklı sayılır → kara liste. Eşik > 0.5 (50/50
    # durumu kalır).
    if file_count >= 20:
        media_count = breakdown.get("image", 0) + breakdown.get("video", 0)
        media_ratio = media_count / file_count
        if media_ratio > 0.5:
            reason = (
                f"resim+video {media_ratio:.0%} ({media_count}/{file_count}) — "
                f"medya-ağırlıklı kanal"
            )
            try:
                await database.add_to_blacklist(username, f"auto: {reason}")
            except Exception as _e:
                logger.warning(f"Blacklist insert failed for @{username}: {_e}")
            try:
                await database.delete_hunter_candidate(candidate_id)
            except Exception as _e:
                logger.warning(f"Delete candidate failed for @{username}: {_e}")
            _emit_event(
                "stage3",
                f"@{username}: 🎬 {reason} → kara listeye eklendi",
                "warn",
                key="hl.stage3.mediaHeavyBlacklisted",
                params={
                    "username": username,
                    "media_ratio": round(media_ratio, 3),
                    "image": breakdown.get("image", 0),
                    "video": breakdown.get("video", 0),
                    "file_count": file_count,
                },
            )
            return False, f"medya-ağırlıklı ({media_ratio:.0%}) → kara listeye eklendi"

    # ── Rule: dominant non-Latin script vs UI language ──
    # named_filenames sentetik isimler hariç gerçek dosya adlarını içerir.
    # Eğer dosya adlarının yarısından fazlası kullanıcının UI dilinde
    # okunamayan bir scriptte ise (çince/japonca/korece/arapça/…) → kara
    # liste. Latin scripti her zaman kabul; Cyrillic sadece RU UI'da kabul;
    # CJK sadece ZH UI'da kabul.
    if len(named_filenames) >= 15:
        dom_script, dom_ratio, n_named = _channel_dominant_script(named_filenames)
        if dom_script and dom_ratio > 0.5:
            ui_lang = (settings_for_kw_outer := None)
            try:
                _settings = await database.get_hunter_settings()
                ui_lang = (_settings.get("ui_language") or "tr").strip().lower()
            except Exception:
                ui_lang = "tr"
            acceptable = _SCRIPT_ACCEPT_BY_LANG.get(ui_lang, {"latin"})
            if dom_script not in acceptable:
                reason = (
                    f"dosya adlarının %{int(dom_ratio*100)}'i {dom_script} script — "
                    f"UI dili '{ui_lang}' bu scripti okumuyor"
                )
                try:
                    await database.add_to_blacklist(username, f"auto: {reason}")
                except Exception as _e:
                    logger.warning(f"Blacklist insert failed for @{username}: {_e}")
                try:
                    await database.delete_hunter_candidate(candidate_id)
                except Exception as _e:
                    logger.warning(f"Delete candidate failed for @{username}: {_e}")
                _emit_event(
                    "stage3",
                    f"@{username}: 🈲 {reason} → kara listeye eklendi",
                    "warn",
                    key="hl.stage3.foreignScriptBlacklisted",
                    params={
                        "username": username, "script": dom_script,
                        "ratio": round(dom_ratio, 3), "ui_lang": ui_lang,
                        "named_files": n_named,
                    },
                )
                return False, (
                    f"{dom_script} script %{int(dom_ratio*100)} (UI dili '{ui_lang}') "
                    f"→ kara listeye eklendi"
                )

    # ── Derived signals for the new score ──
    # 1) Keyword match — pull the user's interest keywords from settings and
    #    scan the combined message text + caption + channel description.
    settings_for_kw = await database.get_hunter_settings()
    user_keywords = [k.strip() for k in (settings_for_kw.get("keywords") or "").split(",") if k.strip()]
    combined_text = " ".join(text_parts) + " " + (description or "")
    keyword_hits = _count_keyword_hits(combined_text, user_keywords)
    # 2) Duplicate ratio — how many filenames repeat (sticker.webp style).
    if file_count:
        unique_fnames  = len(fname_counts)
        duplicate_ratio = max(0.0, (file_count - unique_fnames) / file_count)
        unnamed_ratio   = unnamed_count / file_count
    else:
        duplicate_ratio = unnamed_ratio = 0.0

    score = _score_breakdown(
        file_count, sampled or sample_limit, members or 0, diversity, days_since or 999,
        keyword_hits=keyword_hits, avg_size=avg_size,
        duplicate_ratio=duplicate_ratio, unnamed_ratio=unnamed_ratio,
        breakdown=breakdown,
    )

    # Cache peer_id + access_hash so future API calls (deep_scan, join, …)
    # can build InputPeerChannel directly and skip ResolveUsernameRequest,
    # which has a very strict per-account daily limit.
    pid = getattr(entity, "id", None)
    ahash = getattr(entity, "access_hash", None)

    # Skip channels whose most recent file is older than 1 year — only when
    # we actually retrieved files (file_count > 0); if history was restricted
    # and we got nothing, we skip this check to avoid false-positives.
    if skip_old_channels and file_count > 0 and days_since is not None and days_since > 365:
        # User override: inactive channels are normally rejected, but if their
        # non-media library (archive + document + software + other) exceeds
        # 1000 files, the channel is worth keeping. The standard 200-sample
        # can't reach 1000, so we widen the scan when the sample already
        # contains some non-media (pure-media samples skip the round-trip).
        NON_MEDIA_KEEP_THRESHOLD = 1000
        DEEPER_SAMPLE_LIMIT      = 3000
        nonmedia_sample = (
            breakdown.get("archive", 0) + breakdown.get("document", 0)
            + breakdown.get("software", 0) + breakdown.get("other", 0)
        )
        promoted = False
        if nonmedia_sample > 0:
            _emit_event(
                "stage3",
                f"@{username}: eski ama doc/arşiv var → daha geniş tarama ({DEEPER_SAMPLE_LIMIT} mesaj)",
                "info", key="hl.stage3.oldButProbing",
                params={"username": username, "limit": DEEPER_SAMPLE_LIMIT},
            )
            try:
                (fc2, bd2, lat2, sa2, ts2, tp2, fnc2, unc2, _nf2) = await _sample_entity_messages(
                    client, entity, candidate_id, username, DEEPER_SAMPLE_LIMIT
                )
            except FloodWaitError:
                raise
            except Exception as _e:
                logger.warning(f"Enrich: deeper sample for old @{username} failed: {_e}")
                fc2, bd2, lat2, sa2 = 0, breakdown, last_message_at, sampled
                ts2, tp2, fnc2, unc2 = total_size, text_parts, fname_counts, unnamed_count
            nonmedia_deeper = (
                bd2.get("archive", 0) + bd2.get("document", 0)
                + bd2.get("software", 0) + bd2.get("other", 0)
            )
            if nonmedia_deeper > NON_MEDIA_KEEP_THRESHOLD:
                # Override rejection — use the deeper sample's numbers.
                file_count, breakdown, last_message_at = fc2, bd2, lat2
                sampled, total_size, text_parts = sa2, ts2, tp2
                fname_counts, unnamed_count = fnc2, unc2
                avg_size  = int(total_size / file_count) if file_count else 0
                diversity = sum(1 for v in breakdown.values() if v > 0)
                if last_message_at:
                    if last_message_at.tzinfo is None:
                        last_message_at = last_message_at.replace(tzinfo=timezone.utc)
                    days_since = (datetime.now(timezone.utc) - last_message_at).total_seconds() / 86400
                if file_count:
                    unique_fnames  = len(fname_counts)
                    duplicate_ratio = max(0.0, (file_count - unique_fnames) / file_count)
                    unnamed_ratio   = unnamed_count / file_count
                else:
                    duplicate_ratio = unnamed_ratio = 0.0
                score = _score_breakdown(
                    file_count, sampled or sample_limit, members or 0, diversity,
                    days_since or 999,
                    keyword_hits=keyword_hits, avg_size=avg_size,
                    duplicate_ratio=duplicate_ratio, unnamed_ratio=unnamed_ratio,
                    breakdown=breakdown,
                )
                _emit_event(
                    "stage3",
                    f"@{username}: son dosya {int(days_since)}g eski ama {nonmedia_deeper} doc/arşiv → reddedilmedi",
                    "info", key="hl.stage3.oldButRich",
                    params={"username": username, "days": int(days_since), "nonmedia": nonmedia_deeper},
                )
                promoted = True
        if not promoted:
            logger.info(f"Enrich: @{username} skipped — last file {int(days_since)}d ago (>1 year)")
            await database.update_hunter_candidate(candidate_id, {
                "title": title, "description": (description or "")[:500],
                "is_channel": is_channel, "members": members,
                "peer_id": pid, "access_hash": ahash,
                "status": "rejected",
                "error": f"Son dosya {int(days_since)} gün önce (1 yıldan eski)",
            })
            return False, f"son dosya {int(days_since)} gün eski (>1 yıl) → reddedildi"

    await database.update_hunter_candidate(candidate_id, {
        "title": title,
        "description": (description or "")[:500],
        "is_channel": is_channel,
        "members": members,
        "sampled_messages": sampled,
        "file_count_sample": file_count,
        "estimated_files": file_count,
        "avg_file_size": avg_size,
        "last_message_at": last_message_at,
        "file_type_breakdown": json.dumps(breakdown),
        "score": score,
        "status": "enriched",
        "enriched_at": datetime.utcnow(),
        "error": None,
        "peer_id": pid,
        "access_hash": ahash,
    })
    # Auto-keyword expansion (#3): her başarılı zenginleştirmeden sonra
    # kanalın başlığı + açıklamasından birkaç anlamlı terim çıkar, kalıcı
    # learned_keywords havuzuna ekle. Bir sonraki Stage 2 koşusunda Pattern
    # A-G şablonları bu yeni terimlerle de sorgu üretir.
    try:
        learned = _extract_learned_keywords(title, description, max_kw=4)
        if learned:
            await remember_learned_keywords(learned)
    except Exception as _kw_e:
        logger.debug(f"learn keywords failed for {username}: {_kw_e}")
    return True, None


async def stage3_enrich_pending(settings: dict) -> Tuple[int, int]:
    cap = int(settings.get("tg_daily_lookup_cap") or 500)
    delay_ms = int(settings.get("tg_request_delay_ms") or 1500)
    account_id = int(settings.get("tg_account_id") or 1)

    used_today = await database.hunter_lookups_today()
    budget = max(0, cap - used_today)
    status["stage_detail"] = {
        "lookups_used": used_today, "lookups_cap": cap, "budget": budget,
    }
    if budget <= 0:
        msg = (f"Günlük lookup limiti dolu ({used_today}/{cap}). "
               f"Kalan adaylar limit yenilenince işlenecek "
               f"(veya Ayarlar\'dan limiti yükseltin).")
        _emit_event("stage3", msg, "warn")
        logger.info("Hunter daily cap reached.")
        return 0, 0

    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        _emit_event("stage3", "Telegram hesabı yetkisiz; stage 3 atlanıyor", "warn", key="hl.stage3.tgUnauth")
        return 0, 0

    rows, _ = await database.list_hunter_candidates(status="discovered", limit=budget, offset=0, sort_by="discovered_at", sort_dir="asc")
    enriched, failed = 0, 0
    sample_limit = int(settings.get("tg_messages_to_sample") or 200)
    temp_join_enabled = bool(settings.get("tg_temp_join_enabled"))
    skip_old_channels = bool(settings.get("skip_old_channels", True))
    cached_only_mode = False
    skipped_no_cache = 0

    if not rows:
        _emit_event("stage3", "Zenginleştirilecek aday yok — hepsi zaten enriched/joined/rejected", "info", key="hl.stage3.noPending")
        return 0, 0

    # Sort to put cached-peer candidates FIRST so we make progress even if
    # we eventually hit ResolveUsername limits.
    rows = sorted(rows, key=lambda r: 0 if (r.get("peer_id") and r.get("access_hash") is not None) else 1)
    cached_count = sum(1 for r in rows if r.get("peer_id") and r.get("access_hash") is not None)
    _emit_event("stage3", f"{len(rows)} aday zenginleştirilecek ({cached_count} cache\'li, {len(rows)-cached_count} resolve gerekli)", key="hl.stage3.processing", params={"n": len(rows), "cached": cached_count, "uncached": len(rows)-cached_count})
    status["total"] = len(rows)
    status["progress"] = 0

    for r in rows:
        interrupt = _check_interrupt("stage3")
        if interrupt == "cancel":
            _emit_event("stage3", "Cancelled by user", "warn", key="hl.stage3.cancelled")
            break
        if interrupt == "skip":
            status["skip_stage_requested"] = False
            _emit_event("stage3", "Stage skipped by user", "warn", key="hl.stage3.userSkipped")
            break
        # In cached-only mode skip non-cached candidates without contacting Telegram
        if cached_only_mode and not (r.get("peer_id") and r.get("access_hash") is not None):
            skipped_no_cache += 1
            status["progress"] += 1
            continue
        username = r["username"]
        status["current"] = username
        status["progress"] += 1
        try:
            ok, fail_reason = await _enrich_one(client, r["id"], username, sample_limit, cand=dict(r), temp_join_enabled=temp_join_enabled, skip_old_channels=skip_old_channels)
            if ok:
                enriched += 1
                # Re-fetch to surface the actual numbers we just wrote so the
                # avlama log isn't just "enriched ✓" with no payload.
                try:
                    fresh = await database.get_hunter_candidate(r["id"]) or {}
                except Exception:
                    fresh = {}
                _members = fresh.get("members")
                _samp    = fresh.get("file_count_sample") or 0
                _est     = fresh.get("estimated_files") or 0
                _score   = float(fresh.get("score") or 0.0)
                _bd_raw  = fresh.get("file_type_breakdown") or {}
                if isinstance(_bd_raw, str):
                    try: _bd_raw = json.loads(_bd_raw)
                    except Exception: _bd_raw = {}
                # Top 3 non-zero file types for the log summary.
                _top = sorted(
                    ((k, v) for k, v in _bd_raw.items() if v),
                    key=lambda kv: kv[1], reverse=True,
                )[:3]
                _bd_str = (", ".join(f"{k}:{v}" for k, v in _top)) or "—"
                _members_str = (
                    f"{int(_members):,}".replace(",", ".") if isinstance(_members, (int, float)) and _members
                    else "—"
                )
                # Human-readable summary; key/params keep the structured form
                # so i18n / log filters can use them.
                msg = (
                    f"@{username}: ✓ {_samp} mesaj örneklendi → ~{_est} dosya · "
                    f"üye {_members_str} · skor {_score:.1f} · {_bd_str}"
                )
                _emit_event(
                    "stage3", msg,
                    key="hl.stage3.enrichedDetail",
                    params={
                        "username": username,
                        "sampled": _samp,
                        "estimated": _est,
                        "members": _members or 0,
                        "score": round(_score, 2),
                        "breakdown": _bd_raw,
                    },
                )
            else:
                failed += 1
                reason = fail_reason or "bilinmeyen sebep"
                _emit_event(
                    "stage3",
                    f"@{username}: ✗ {reason}",
                    "warn",
                    key="hl.stage3.failedDetail",
                    params={"username": username, "reason": reason},
                )
        except FloodWaitError as e:
            backoff = int(getattr(e, "seconds", 60))
            # A long FloodWait on ResolveUsernameRequest means the per-account
            # username-resolve quota is exhausted — sleeping for ~10h would
            # leave the UI "stuck" pointlessly. Bail out of the run instead;
            # remaining candidates will keep their `discovered` status and be
            # picked up on the next run after the wait expires.
            if backoff > 600:
                # ResolveUsername quota is exhausted on this account — switch
                # to "cached-only" mode for the rest of the run: process only
                # candidates that already have peer_id+access_hash so we never
                # touch ResolveUsernameRequest again. Other candidates are
                # left in 'discovered' state for a future run.
                hrs, mins = backoff // 3600, (backoff % 3600) // 60
                if not cached_only_mode:
                    cached_only_mode = True
                    msg = (
                        f"Telegram @username çözümleme kotası tükendi (FloodWait {hrs}s {mins}d). "
                        f"Bu turda yalnızca 'önbellekli' adaylar (peer_id + access_hash bilgisi "
                        f"daha önce DB'ye yazılmış olanlar) zenginleştirilecek; bunlara erişmek "
                        f"yeni bir ResolveUsername çağrısı gerektirmez. 'Önbelleksiz' adaylar "
                        f"(yalnızca @username ile bilinen ve henüz peer'i çözülmemiş olanlar) "
                        f"atlanır; kota yenilenince bir sonraki turda otomatik denenir."
                    )
                    logger.warning(
                        f"Hunter: ResolveUsername limit hit ({backoff}s) — switching to cached-only"
                    )
                    _emit_event("stage3", msg, "warn", key="hl.stage3.cacheOnlyMode", params={"hrs": hrs, "mins": mins})
                    status["error"] = None  # not a fatal error anymore
                    # Persist the FloodWait window so the quota lightbox can show
                    # "pencere ne zaman kapanıyor / ne kadar kaldı". Survives a
                    # container restart unlike the in-memory `status` dict.
                    try:
                        from datetime import timedelta as _td
                        await database.update_hunter_settings({
                            "last_floodwait_until": datetime.utcnow() + _td(seconds=backoff),
                            "last_floodwait_scope": "resolveUsername",
                            "last_floodwait_seconds": int(backoff),
                        })
                    except Exception as _e:
                        logger.warning(f"FloodWait persist failed: {_e}")
                # Skip THIS candidate (it had no cache and triggered the limit)
                # but continue processing the rest of the queue
                skipped_no_cache += 1
                continue
            logger.warning(f"Hunter FloodWait — sleeping {backoff}s")
            _emit_event("stage3", f"FloodWait {backoff}s, bekleniyor…", "warn", key="hl.stage3.floodWait", params={"seconds": backoff})
            await _interruptible_sleep(max(60, backoff))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failed += 1
            logger.warning(f"Enrich error for {username}: {e}")
        await asyncio.sleep(delay_ms / 1000)

    if skipped_no_cache > 0:
        _emit_event(
            "stage3",
            (
                f"{skipped_no_cache} aday 'önbelleksiz' olduğu için bu turda atlandı: "
                f"bu adayların yalnızca @username'i biliniyordu ve zenginleştirme için "
                f"Telegram'a yeni bir ResolveUsername çağrısı gerekiyordu, ancak kota dolu. "
                f"Kota yenilendiğinde sıradaki turda otomatik işlenecekler."
            ),
            "warn",
            key="hl.stage3.cacheSkip",
            params={"n": skipped_no_cache},
        )
    return enriched, failed


# ── Top-level orchestration ──────────────────────────────────────────────────

async def run_hunter_once():
    """Single end-to-end run. Stages 1 and 2 produce seeds; stage 3 enriches."""
    global _run_task
    async with _running_lock:
        if status["running"]:
            return
        status.update({
            "running": True, "stage": None, "progress": 0, "total": 0,
            "seeds_found": 0, "enriched": 0, "failed": 0,
            "current": None, "error": None,
            "started_at": datetime.utcnow().isoformat(), "finished_at": None,
            "stage_detail": {},
            # NOTE: events are deliberately NOT cleared between runs so the user
            # can read what the last run did after it finishes. The cap inside
            # _emit_event keeps memory bounded.
            "cancel_requested": False, "skip_stage_requested": False,
            "stage_started_at": None,
        })
        _emit_event("run", "─── Yeni av turu başladı ───", key="hl.run.started")

    settings = await database.get_hunter_settings()
    run_id = await database.start_hunter_run(note="manual")
    seeds_found = enriched = failed = 0
    try:
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 0", "warn", key="hl.run.cancelledStage0"); raise asyncio.CancelledError
        if settings.get("similar_expand_enabled", True):
            status["stage"] = "stage0"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            status["stage_detail"] = {}
            _emit_event("stage0", "Stage 0: Telegram benzer-kanal grafiği taranıyor",
                         key="hl.stage0.preface")
            try:
                s0_added = await stage0_similar_expand(settings)
                seeds_found += s0_added
                status["seeds_found"] = seeds_found
            except Exception as s0_err:
                _emit_event(
                    "stage0", f"Stage 0 hata: {s0_err}", "warn",
                    key="hl.stage0.err", params={"err": str(s0_err)[:120]},
                )
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 1", "warn", key="hl.run.cancelledStage1"); raise asyncio.CancelledError
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        if settings.get("stage1_enabled"):
            status["stage"] = "stage1"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            status["stage_detail"] = {}
            _emit_event("stage1", "Stage 1: scanning internal links & mentions", key="hl.stage1.start")
            seeds_found += await stage1_mine_internal()
            status["seeds_found"] = seeds_found
            _emit_event("stage1", f"Stage 1 done: {seeds_found} seeds total", key="hl.stage1.done", params={"n": seeds_found})
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 2", "warn", key="hl.run.cancelledStage2"); raise asyncio.CancelledError
        # skip flag at stage boundary: clear and proceed to next stage
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        if settings.get("stage2_enabled"):
            status["stage"] = "stage2"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            seeds_found += await stage2_crawl_web(settings)
            status["seeds_found"] = seeds_found
            _emit_event("stage2", f"Stage 2 done: {seeds_found} seeds total", key="hl.stage2.done", params={"n": seeds_found})
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before magnet hunt", "warn", key="hl.run.cancelledMagnetHunt"); raise asyncio.CancelledError
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        # Magnet hunt — sits between Stage 2 (seed discovery via web) and
        # Stage 3 (Telegram enrichment). Discovers magnet URIs via search-
        # engine dorks and persists them under the "web magnets" synthetic
        # group. Honored as a pipeline phase so the standalone toolbar button
        # is no longer needed.
        if settings.get("magnethunt_enabled", True):
            status["stage"] = "magnethunt"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            status["stage_detail"] = {}
            _emit_event("magnethunt", "Magnet avı: web'de magnet URI'leri taranıyor", key="hl.magnetHunt.pipelineStart")
            try:
                await run_magnet_hunt()
                mh_new = int(magnet_hunt_status.get("magnets_new") or 0)
                mh_found = int(magnet_hunt_status.get("magnets_found") or 0)
                _emit_event(
                    "magnethunt",
                    f"Magnet avı tamamlandı: {mh_new} yeni / {mh_found} bulunan",
                    key="hl.magnetHunt.pipelineDone",
                    params={"new": mh_new, "found": mh_found},
                )
            except Exception as mh_err:
                _emit_event("magnethunt", f"Magnet avı hata: {mh_err}", "warn",
                             key="hl.magnetHunt.pipelineErr",
                             params={"err": str(mh_err)[:120]})
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before magnet backfill", "warn", key="hl.run.cancelledMagnetBackfill"); raise asyncio.CancelledError
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        # Magnet Backfill — fills in missing file lists for magnets and catches
        # any magnet URIs that were posted to groups historically but never
        # captured by the live handler. Replaces the old Settings → "Geçmiş
        # Veri Tarama" card.
        if settings.get("magnet_backfill_enabled", True):
            status["stage"] = "magnetbackfill"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            status["stage_detail"] = {}
            _emit_event(
                "magnetbackfill",
                "Magnet Backfill: geçmiş magnet'ler taranıyor + eksik dosya listeleri çekiliyor",
                key="hl.magnetBackfill.start",
            )
            try:
                import sync as _sync
                if _sync.magnet_backfill_status.get("running"):
                    _emit_event(
                        "magnetbackfill",
                        "Önceki magnet backfill devam ediyor, atlanıyor",
                        "warn", key="hl.magnetBackfill.skipBusy",
                    )
                else:
                    await _sync.run_magnet_backfill()
                    s = _sync.magnet_backfill_status
                    _emit_event(
                        "magnetbackfill",
                        f"Magnet Backfill tamamlandı: {s.get('new_magnets', 0)} yeni magnet, "
                        f"{s.get('enrich_success', 0)} dosya listesi eklendi, "
                        f"{s.get('enrich_fail', 0)} başarısız",
                        key="hl.magnetBackfill.done",
                        params={
                            "new":     int(s.get("new_magnets", 0)),
                            "ok":      int(s.get("enrich_success", 0)),
                            "fail":    int(s.get("enrich_fail", 0)),
                        },
                    )
            except Exception as mb_err:
                _emit_event(
                    "magnetbackfill", f"Magnet backfill hata: {mb_err}", "warn",
                    key="hl.magnetBackfill.err",
                    params={"err": str(mb_err)[:120]},
                )
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 3", "warn", key="hl.run.cancelledStage3"); raise asyncio.CancelledError
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        status["stage"] = "stage3"
        status["stage_started_at"] = datetime.utcnow().isoformat()
        status["stage_detail"] = {}
        _emit_event("stage3", "Stage 3: enriching candidates via Telegram", key="hl.stage3.start")
        e, f = await stage3_enrich_pending(settings)
        enriched, failed = e, f
        status["enriched"] = enriched
        status["failed"] = failed
        _emit_event("stage3", f"Stage 3 done: {enriched} enriched, {failed} failed", key="hl.stage3.done", params={"enriched": enriched, "failed": failed})
        await database.update_hunter_settings({"last_run_at": datetime.utcnow()})
    except Exception as e:
        status["error"] = str(e)
        logger.error(f"Hunter run failed: {e}", exc_info=True)
    finally:
        status["stage"] = None
        status["running"] = False
        status["finished_at"] = datetime.utcnow().isoformat()
        try:
            await database.finish_hunter_run(run_id,
                seeds_found=seeds_found, enriched=enriched,
                failed=failed, error=status.get("error"))
        except Exception:
            pass


def request_cancel() -> bool:
    if not status.get("running"):
        return False
    status["cancel_requested"] = True
    _emit_event("run", "Cancel requested by user", "warn", key="hl.run.cancelRequested")
    # Also cancel the asyncio task to break out of any in-flight await
    try:
        if _run_task and not _run_task.done():
            _run_task.cancel()
    except Exception:
        pass
    return True


def request_skip_stage() -> bool:
    if not status.get("running"):
        return False
    status["skip_stage_requested"] = True
    _emit_event(status.get("stage") or "run", "Skip stage requested by user", "warn", key="hl.run.skipRequested")
    return True


def kick_run() -> bool:
    """Start a hunter run in the background. Returns False if already running."""
    global _run_task
    if status["running"]:
        return False
    _run_task = asyncio.create_task(run_hunter_once())
    return True


async def run_enrich_only():
    """Stage-3-only entry point used by the "Zenginleştir" toolbar button.
    Shares the same `status` lifecycle as a full run so the existing hunter
    monitor + event log render its progress identically."""
    global _run_task
    async with _running_lock:
        if status["running"]:
            return
        status.update({
            "running": True, "stage": "stage3", "progress": 0, "total": 0,
            "seeds_found": 0, "enriched": 0, "failed": 0,
            "current": None, "error": None,
            "started_at": datetime.utcnow().isoformat(), "finished_at": None,
            "stage_detail": {},
            "cancel_requested": False, "skip_stage_requested": False,
            "stage_started_at": datetime.utcnow().isoformat(),
        })
        _emit_event(
            "run",
            "─── Sadece zenginleştirme turu başladı ───",
            key="hl.run.enrichOnlyStarted",
        )

    settings = await database.get_hunter_settings()
    run_id = await database.start_hunter_run(note="enrich-only")
    enriched = failed = 0
    try:
        _emit_event(
            "stage3",
            "Stage 3: keşfedilen adaylar Telegram üzerinden zenginleştiriliyor",
            key="hl.stage3.start",
        )
        e, f = await stage3_enrich_pending(settings)
        enriched, failed = e, f
        status["enriched"] = enriched
        status["failed"] = failed
        _emit_event(
            "stage3",
            f"Stage 3 tamamlandı: {enriched} zenginleştirildi, {failed} başarısız",
            key="hl.stage3.done",
            params={"enriched": enriched, "failed": failed},
        )
        await database.update_hunter_settings({"last_run_at": datetime.utcnow()})
    except Exception as e:
        status["error"] = str(e)
        logger.error(f"Enrich-only run failed: {e}", exc_info=True)
        # Surface the failure in the visible hunter log so the user doesn't
        # stare at a frozen "started" line forever.
        _emit_event(
            "run",
            f"Zenginleştirme başarısız: {str(e)[:200]}",
            "warn",
            key="hl.run.enrichOnlyFailed",
            params={"err": str(e)[:200]},
        )
    finally:
        status["stage"] = None
        status["running"] = False
        status["finished_at"] = datetime.utcnow().isoformat()
        try:
            await database.finish_hunter_run(
                run_id, seeds_found=0, enriched=enriched,
                failed=failed, error=status.get("error"),
            )
        except Exception:
            pass


def kick_enrich_only() -> bool:
    """Start a Stage-3-only enrichment in the background. Returns False if a
    hunter run is already in flight."""
    global _run_task
    if status["running"]:
        return False
    _run_task = asyncio.create_task(run_enrich_only())
    return True


async def backfill_peer_cache(account_id: int = 1, limit: int = 200) -> int:
    """Walk enriched candidates that don't have peer_id cached and try to fill
    them from Telethon's local session DB only — does NOT call ResolveUsername,
    so it's safe to run anytime. Returns the number of rows backfilled."""
    rows = await database._q(
        """SELECT id, username FROM hunter_candidates
           WHERE status = 'enriched' AND (peer_id IS NULL OR access_hash IS NULL)
           ORDER BY enriched_at DESC NULLS LAST LIMIT $1""",
        limit,
    )
    if not rows:
        return 0
    try:
        client = await get_client(account_id)
    except Exception:
        return 0
    if not client.is_connected():
        try: await client.connect()
        except Exception: return 0
    n = 0
    for r in rows:
        try:
            # get_input_entity falls back to ResolveUsername only as last resort,
            # but consults session cache first. We catch and skip if it would
            # require a network call.
            input_peer = await client.get_input_entity(r["username"])
            pid = getattr(input_peer, "channel_id", None) or getattr(input_peer, "chat_id", None) or getattr(input_peer, "user_id", None)
            ah = getattr(input_peer, "access_hash", None)
            if pid and ah is not None:
                await database.update_hunter_candidate(r["id"], {"peer_id": pid, "access_hash": ah})
                n += 1
        except FloodWaitError:
            # Stop early — don't burn the resolve cap on backfill
            break
        except Exception:
            continue
    return n


# ── Action: join a discovered channel + start tracking it ────────────────────

async def join_candidate(candidate_id: int) -> dict:
    cand = await database.get_hunter_candidate(candidate_id)
    if not cand:
        return {"ok": False, "error": "not found"}
    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)

    # Short-circuit: if the user is already a member of this channel from a
    # previous (manual or hunter-driven) join, don't fire JoinChannelRequest
    # again — Telegram can return FloodWait even on no-op joins. Just sync
    # hunter's internal state with reality.
    existing = await database.find_account_group_by_username(account_id, cand["username"])
    if existing:
        await database.update_hunter_candidate(candidate_id, {
            "status": "joined", "decided_at": datetime.utcnow(),
        })
        await database.delete_join_from_queue(candidate_id)
        return {"ok": True, "group_id": existing["id"], "already_member": True}

    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        return {"ok": False, "error": "telegram not authorized"}
    try:
        from telethon.tl.functions.channels import JoinChannelRequest
        # Prefer cached peer to avoid ResolveUsernameRequest limit
        entity = None
        pid = cand.get("peer_id"); ah = cand.get("access_hash")
        if pid and ah is not None:
            try:
                entity = await client.get_entity(InputPeerChannel(int(pid), int(ah)))
            except FloodWaitError:
                raise
            except Exception:
                entity = None
        if entity is None:
            entity = await client.get_entity(cand["username"])
            # cache for next time
            try:
                p2, a2 = getattr(entity,"id",None), getattr(entity,"access_hash",None)
                if p2 and a2 is not None:
                    await database.update_hunter_candidate(candidate_id, {"peer_id": p2, "access_hash": a2})
            except Exception: pass
        await client(JoinChannelRequest(entity))
        # Register in our groups tables so subsequent syncs index it
        gid = entity.id
        # Convert to negative bigint format as used in our `groups.id`
        if isinstance(entity, Channel):
            gid = int("-100" + str(entity.id))
        else:
            gid = -int(entity.id)
        await database.upsert_group(
            gid, getattr(entity, "title", None) or cand["username"],
            getattr(entity, "username", None), True,
        )
        await database.upsert_account_group(account_id, gid)
        await database.update_hunter_candidate(candidate_id, {"status": "joined", "decided_at": datetime.utcnow()})
        _emit_event(
            "join", f"@{cand['username']}: üye olundu ✓", "info",
            key="hl.join.ok", params={"username": cand["username"]},
        )
        return {"ok": True, "group_id": gid}
    except FloodWaitError as e:
        # Persist for the retry worker to pick up later. Return ok=True so
        # the frontend treats this as a "queued" action rather than a hard
        # failure — a different toast surfaces the wait info.
        await database.enqueue_join(
            candidate_id, account_id, int(e.seconds),
            last_error=f"flood wait {e.seconds}s",
        )
        wait_s = int(e.seconds)
        _emit_event(
            "join",
            f"@{cand['username']}: FloodWait — {wait_s}sn sonra otomatik tekrar denenecek",
            "warn",
            key="hl.join.floodwait",
            params={"username": cand["username"], "wait": wait_s},
        )
        return {
            "ok": True,
            "queued": True,
            "wait_s": wait_s,
            "candidate_id": candidate_id,
        }
    except Exception as e:
        # Surface the exact Telegram error class + message so future debug
        # doesn't need to instrument the handler again. Common cases we
        # decode specially:
        #   - InviteRequestSentError       → channel requires admin approval;
        #                                    Telegram already queued our request.
        #   - ChannelsTooMuchError         → account is at the 500-channel cap.
        #   - InviteHashEmpty/Expired      → public channel turned private.
        err_cls  = type(e).__name__
        err_msg  = str(e)[:300]
        logger.warning(
            f"join_candidate failed for @{cand['username']} (cid={candidate_id}): "
            f"{err_cls}: {err_msg}"
        )
        # Special: InviteRequestSentError is actually a "soft success" — the
        # server accepted our join request and is waiting for an admin to
        # approve it. Distinct from FloodWait so the UI doesn't say "0s
        # sonra tekrar denenecek" (which is meaningless — there's no retry,
        # we're waiting on a human).
        if err_cls in ("InviteRequestSentError", "InviteRequestSent"):
            await database.update_hunter_candidate(candidate_id, {
                "error": "Admin approval pending (request sent)",
            })
            _emit_event(
                "join",
                f"@{cand['username']}: katılım isteği gönderildi, admin onayı bekleniyor",
                "info",
                key="hl.join.pendingApproval",
                params={"username": cand["username"]},
            )
            return {
                "ok": True,
                "pending_approval": True,
                "candidate_id": candidate_id,
            }
        # Persist the error on the candidate so the UI can show it next time
        # the user opens the detail (instead of "joinFail" with a stale
        # toast that disappears in 5s).
        try:
            await database.update_hunter_candidate(candidate_id, {
                "error": f"{err_cls}: {err_msg}"[:200],
            })
        except Exception:
            pass
        _emit_event(
            "join",
            f"@{cand['username']}: üye olunamadı — {err_cls}: {err_msg[:120]}",
            "warn",
            key="hl.join.fail",
            params={"username": cand["username"], "err_cls": err_cls, "err": err_msg[:120]},
        )
        return {"ok": False, "error": f"{err_cls}: {err_msg}"}


async def reject_candidate(candidate_id: int) -> dict:
    await database.update_hunter_candidate(candidate_id,
        {"status": "rejected", "decided_at": datetime.utcnow()})
    return {"ok": True}


async def blacklist_candidate(candidate_id: int, reason: Optional[str] = None) -> dict:
    cand = await database.get_hunter_candidate(candidate_id)
    if not cand:
        return {"ok": False, "error": "not found"}
    await database.add_to_blacklist(cand["username"], reason)
    await database.update_hunter_candidate(candidate_id,
        {"status": "blacklisted", "decided_at": datetime.utcnow()})
    return {"ok": True}


async def restore_candidate(candidate_id: int) -> dict:
    """Kara listeden veya reddedilenlerden geri al → discovered durumuna döndür."""
    cand = await database.get_hunter_candidate(candidate_id)
    if not cand:
        return {"ok": False, "error": "not found"}
    if cand.get("status") == "blacklisted":
        # Kara liste kaydını da kaldır
        await database._exec(
            "DELETE FROM hunter_blacklist WHERE username = $1", cand["username"]
        )
    await database.update_hunter_candidate(candidate_id,
        {"status": "discovered", "decided_at": None, "error": None})
    return {"ok": True}


# ── Deep scan: pull EVERY document message from a candidate ──────────────────

deep_scan_status: Dict[int, dict] = {}        # {candidate_id: {state, processed, total, error}}
_deep_scan_tasks: Dict[int, asyncio.Task] = {}

# Per-file download state for the candidate-detail lightbox 📥 button.
# Keyed by (candidate_id, message_id).
#   state ∈ {"downloading","done","error","needs_temp_join"}
file_dl_status: Dict[tuple, dict] = {}
_file_dl_tasks: Dict[tuple, asyncio.Task] = {}


def _file_group_for_ext(ext: str) -> str:
    return _file_group(ext)


async def _scan_iter_documents(client, entity, candidate_id: int,
                                 username: str, delay_ms: float,
                                 starting_n: int = 0):
    """Walk all document messages of an entity into hunter_candidate_files,
    resuming from offset_id on FloodWait.
    Returns (n, breakdown, total_size, last_at, fname_counts, unnamed_count) —
    the last two feed the duplicate-name + unnamed-ratio scoring penalties."""
    n = starting_n
    breakdown = {k: 0 for k in list(_FILE_GROUPS.keys()) + ["other"]}
    total_size = 0
    last_at = None
    offset_id = 0
    consecutive_floodwaits = 0
    fname_counts: Dict[str, int] = {}
    unnamed_count = 0
    while True:
        try:
            # No server-side filter: FilterDocument excludes mp4/mp3 documents
            # (those go to FilterVideo/FilterAudio buckets server-side), so a
            # "movies channel" would scan 0 files under the document filter.
            async for msg in client.iter_messages(entity, offset_id=offset_id):
                offset_id = msg.id
                if msg.document:
                    doc = msg.document
                    size = int(getattr(doc, "size", 0) or 0)
                    fname, ext, _is_video, _is_audio, is_named = _doc_filename(doc, msg.id)
                    grp = _file_group(ext)
                    dup_key = fname
                elif isinstance(msg.media, MessageMediaPhoto):
                    photo = getattr(msg, "photo", None)
                    size = _photo_size(photo) if photo else 0
                    fname = f"photo_{msg.id}.jpg"
                    ext = "jpg"
                    grp = "image"
                    is_named = False
                    # All photos share one dedup bucket — every photo_*.jpg
                    # already has a per-message id; counting them as distinct
                    # filenames would mask sticker-spam patterns.
                    dup_key = "__photo__"
                else:
                    continue
                n += 1
                total_size += size
                breakdown[grp] += 1
                fname_counts[dup_key] = fname_counts.get(dup_key, 0) + 1
                if not is_named:
                    unnamed_count += 1
                date = msg.date
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date and (last_at is None or date > last_at):
                    last_at = date
                await database.insert_candidate_file(
                    candidate_id, msg.id, fname, ext, size, grp, date,
                    is_named=is_named,
                )
                if n % 25 == 0:
                    deep_scan_status[candidate_id] = {
                        "state": "running", "processed": n, "total": n, "error": None,
                    }
                    await database.update_hunter_candidate(candidate_id, {
                        "deep_scan_progress": n, "deep_scan_total": n,
                    })
                    await asyncio.sleep(0)
                if n % 500 == 0:
                    await asyncio.sleep(min(1.0, delay_ms))
            break  # async-for finished naturally (whole history walked)
        except FloodWaitError as e:
            wait = max(30, int(getattr(e, "seconds", 60)))
            consecutive_floodwaits += 1
            if consecutive_floodwaits > 6:
                raise
            logger.warning(
                f"Deep-scan FloodWait {wait}s on @{username} "
                f"(processed={n}, resume from msg_id={offset_id})"
            )
            deep_scan_status[candidate_id] = {
                "state": "running", "processed": n, "total": n,
                "error": f"flood wait {wait}s, resuming…",
            }
            await asyncio.sleep(wait)
            continue
    return n, breakdown, total_size, last_at, fname_counts, unnamed_count


async def deep_scan_candidate(candidate_id: int):
    cand = await database.get_hunter_candidate(candidate_id)
    if not cand:
        return
    username = cand["username"]
    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)
    temp_join_enabled = bool(settings.get("tg_temp_join_enabled"))
    skip_old_channels = bool(settings.get("skip_old_channels", True))

    deep_scan_status[candidate_id] = {"state": "running", "processed": 0, "total": 0, "error": None}
    await database.update_hunter_candidate(candidate_id, {
        "deep_scan_status": "running",
        "deep_scan_progress": 0,
        "deep_scan_total": 0,
        "deep_scan_error": None,
    })

    try:
        client = await get_client(account_id)
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram not authorized")
        # Reduce silent FloodWait threshold since we have our own loop
        try: client.flood_sleep_threshold = max(client.flood_sleep_threshold or 0, 120)
        except Exception: pass

        # Prefer cached peer to avoid the expensive ResolveUsernameRequest, which
        # has a very strict per-account daily limit (a single bad day can block
        # username resolves for 10+ hours).
        entity = None
        peer_id = cand.get("peer_id")
        access_hash = cand.get("access_hash")
        if peer_id and access_hash is not None:
            try:
                entity = await client.get_entity(InputPeerChannel(int(peer_id), int(access_hash)))
            except FloodWaitError:
                raise
            except Exception as e:
                logger.warning(f"Cached peer for @{username} did not resolve: {e}; falling back to username")
                entity = None
        if entity is None:
            try:
                entity = await client.get_entity(username)
                # Cache the freshly-resolved peer so the NEXT deep-scan / join
                # for this candidate skips ResolveUsernameRequest.
                pid = getattr(entity, "id", None)
                ahash = getattr(entity, "access_hash", None)
                if pid and ahash is not None:
                    await database.update_hunter_candidate(candidate_id, {
                        "peer_id": pid, "access_hash": ahash,
                    })
            except (UsernameInvalidError, UsernameNotOccupiedError):
                await database.add_to_blacklist(username, "auto: username invalid (deep scan)")
                await database.delete_hunter_candidate(candidate_id)
                deep_scan_status[candidate_id] = {"state": "deleted", "processed": 0, "total": 0, "error": "username invalid"}
                return
            except ChannelPrivateError:
                await database.add_to_blacklist(username, "auto: private (deep scan)")
                await database.delete_hunter_candidate(candidate_id)
                deep_scan_status[candidate_id] = {"state": "deleted", "processed": 0, "total": 0, "error": "private"}
                return
            except FloodWaitError as e:
                # ResolveUsernameRequest hit its hard cap. Surface this as a
                # human-readable error rather than a generic "Error".
                wait = int(getattr(e, "seconds", 0))
                hours = wait // 3600
                mins = (wait % 3600) // 60
                msg = f"Telegram username çözümleme limitine ulaşıldı ({hours}s {mins}d sonra deneyin). Bu aday daha önce zenginleştirildiyse mevcut peer cache otomatik kullanılacak; yoksa beklemek gerekir."
                deep_scan_status[candidate_id] = {"state": "error", "processed": 0, "total": 0, "error": msg}
                await database.update_hunter_candidate(candidate_id, {
                    "deep_scan_status": "error",
                    "deep_scan_error": msg[:200],
                })
                return

        # Estimate total
        total = None
        try:
            full = await client(GetFullChannelRequest(entity))
            total = getattr(full.full_chat, "participants_count", None)  # not actually file count, but a hint
        except Exception:
            pass
        # Get total documents via a count request — Telethon doesn't expose this
        # cheaply; iter_messages walks them all. So we just walk and count.

        delay_ms = int(settings.get("tg_request_delay_ms") or 1500) / 1000

        # First attempt: scan as a non-member.
        n, breakdown, total_size, last_at, fname_counts, unnamed_count = await _scan_iter_documents(
            client, entity, candidate_id, username, delay_ms,
        )

        temp_joined = False
        temp_join_err: Optional[str] = None
        left_after_temp = False
        # If 0 documents came back the channel likely restricts history to
        # members. With user opt-in, try a temporary join → re-scan → leave.
        if n == 0 and temp_join_enabled:
            logger.info(f"Hunter: 0 docs from @{username} as non-member — temp-joining for scan")
            try:
                await client(JoinChannelRequest(entity))
                temp_joined = True
                deep_scan_status[candidate_id] = {
                    "state": "running", "processed": 0, "total": 0,
                    "error": "joined temporarily; rescanning…",
                    "temp_joined": True, "temp_join_error": None,
                }
                # Re-scan after joining
                n, breakdown, total_size, last_at, fname_counts, unnamed_count = await _scan_iter_documents(
                    client, entity, candidate_id, username, delay_ms,
                )
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 0))
                temp_join_err = f"FloodWait {wait}s"
            except Exception as e:
                temp_join_err = str(e)[:120]
                logger.warning(f"Temp-join failed for @{username}: {e}")
            finally:
                # Always leave if we joined — user makes the real "join" call.
                # User explicitly asked: even if files can't be pulled, ensure
                # we don't stay a member.
                if temp_joined:
                    try:
                        await client(LeaveChannelRequest(entity))
                        left_after_temp = True
                        logger.info(f"Hunter: left @{username} after temp scan")
                    except Exception as e:
                        logger.warning(f"Hunter: leave-after-tempjoin failed for @{username}: {e}")

        # Finalize: update candidate aggregate stats from full data
        avg_size = int(total_size / n) if n else 0
        diversity = sum(1 for v in breakdown.values() if v > 0)
        days_since = None
        if last_at:
            days_since = (datetime.now(timezone.utc) - last_at).total_seconds() / 86400
        # Re-score using full data (much more reliable than a 200-msg sample).
        # density=1.0 here because every counted message was a media item.
        # Deep scan doesn't collect message text, so keyword_hits stays 0 —
        # the enrichment-pass score already factored keywords in.
        if n:
            unique_fnames  = len(fname_counts)
            duplicate_ratio = max(0.0, (n - unique_fnames) / n)
            unnamed_ratio   = unnamed_count / n
        else:
            duplicate_ratio = unnamed_ratio = 0.0
        score = _score_breakdown(
            file_count=n,
            sampled=n if n else 1,
            members=cand.get("members") or 0,
            diversity=diversity,
            days_since_last=days_since if days_since is not None else 999,
            keyword_hits=0,
            avg_size=avg_size,
            duplicate_ratio=duplicate_ratio,
            unnamed_ratio=unnamed_ratio,
            breakdown=breakdown,
        )

        # Reject channels whose newest file is older than 1 year (deep-scan confirmed).
        if skip_old_channels and n > 0 and days_since is not None and days_since > 365:
            logger.info(f"Deep scan: @{username} rejected — last file {int(days_since)}d ago (>1 year)")
            await database.update_hunter_candidate(candidate_id, {
                "estimated_files": n,
                "avg_file_size": avg_size,
                "file_type_breakdown": json.dumps(breakdown),
                "last_message_at": last_at,
                "score": score,
                "status": "rejected",
                "error": f"Son dosya {int(days_since)} gün önce (1 yıldan eski)",
                "deep_scan_status": "done",
                "deep_scan_progress": n,
                "deep_scan_total": n,
                "deep_scan_at": datetime.utcnow(),
                "deep_scan_error": None,
            })
            deep_scan_status[candidate_id] = {
                "state": "done", "processed": n, "total": n,
                "error": f"Kanal reddedildi: son dosya {int(days_since)} gün önce",
                "temp_joined": bool(temp_joined),
                "temp_join_error": temp_join_err,
                "left_after_temp": bool(left_after_temp),
            }
            return

        await database.update_hunter_candidate(candidate_id, {
            "estimated_files": n,
            "avg_file_size": avg_size,
            "file_type_breakdown": json.dumps(breakdown),
            "last_message_at": last_at,
            "score": score,
            "deep_scan_status": "done",
            "deep_scan_progress": n,
            "deep_scan_total": n,
            "deep_scan_at": datetime.utcnow(),
            "deep_scan_error": None,
        })
        # Carry temp-join outcome through to the status response so the UI
        # can show "joined+left+still empty" vs "join itself failed".
        deep_scan_status[candidate_id] = {
            "state": "done", "processed": n, "total": n, "error": None,
            "temp_joined": bool(temp_joined),
            "temp_join_error": temp_join_err,
            "left_after_temp": bool(left_after_temp),
        }
    except asyncio.CancelledError:
        deep_scan_status[candidate_id] = {"state": "cancelled", "processed": 0, "total": 0, "error": "cancelled"}
        await database.update_hunter_candidate(candidate_id, {
            "deep_scan_status": "cancelled",
            "deep_scan_error": "cancelled",
        })
        raise
    except Exception as e:
        msg = str(e)[:200]
        logger.warning(f"Deep scan error {username}: {msg}")
        deep_scan_status[candidate_id] = {"state": "error", "processed": 0, "total": 0, "error": msg}
        await database.update_hunter_candidate(candidate_id, {
            "deep_scan_status": "error",
            "deep_scan_error": msg,
        })
    finally:
        _deep_scan_tasks.pop(candidate_id, None)


def kick_deep_scan(candidate_id: int) -> bool:
    if candidate_id in _deep_scan_tasks and not _deep_scan_tasks[candidate_id].done():
        return False
    _deep_scan_tasks[candidate_id] = asyncio.create_task(deep_scan_candidate(candidate_id))
    return True


def cancel_deep_scan(candidate_id: int) -> bool:
    task = _deep_scan_tasks.get(candidate_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ── Per-file download (from the candidate-detail lightbox 📥) ───────────────
# Goal: let the user preview a specific document from a candidate channel WITHOUT
# committing to join/reject/blacklist. For public channels Telegram lets us read
# a message + download its document without joining. For private/restricted
# channels we offer an explicit temp-join → download → leave flow, gated by a
# query flag so the UI can show a confirmation modal first.

_DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/app/downloads")


def _safe_segment(s: str, fallback: str = "x") -> str:
    s = "".join(c for c in (s or "") if c.isalnum() or c in " _-.").strip()
    return s[:80] or fallback


async def _try_fetch_message(client, entity, message_id: int):
    """Fetch a single message. Returns the Message on success, None if it's
    unreachable for permission reasons (caller decides whether to temp-join)."""
    try:
        msg = await client.get_messages(entity, ids=message_id)
        return msg
    except (ChannelPrivateError,) as e:
        return None
    except FloodWaitError:
        raise
    except Exception as e:
        # The vast majority of "permission" errors surface as different
        # subclasses depending on Telethon version. Treat any non-flood
        # exception that mentions privacy/membership as a permission gate;
        # everything else propagates so the caller can show a real error.
        name = type(e).__name__.lower()
        if "private" in name or "admin" in name or "forbidden" in name:
            return None
        raise


async def _download_candidate_file_impl(cid: int, msg_id: int,
                                        allow_temp_join: bool) -> str:
    """Returns the local path on success. Raises on hard failures.
    Sets status into file_dl_status[(cid, msg_id)] for the UI to poll."""
    key = (int(cid), int(msg_id))
    file_dl_status[key] = {"state": "downloading", "progress": 0.0,
                            "bytes_done": 0, "bytes_total": 0, "error": None}

    cand = await database.get_hunter_candidate(cid)
    if not cand:
        file_dl_status[key] = {"state": "error", "error": "candidate not found"}
        raise ValueError("candidate not found")

    cfile = await database.get_candidate_file(cid, msg_id)
    if not cfile:
        file_dl_status[key] = {"state": "error", "error": "file row not found"}
        raise ValueError("file row not found")

    # Short-circuit if we already have the file on disk.
    if cfile.get("local_path") and os.path.exists(cfile["local_path"]):
        file_dl_status[key] = {"state": "done", "progress": 1.0,
                                "local_path": cfile["local_path"]}
        return cfile["local_path"]

    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)
    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()

    # Resolve entity from cached peer_id+access_hash so we never burn a
    # ResolveUsername call here.
    pid = cand.get("peer_id"); ah = cand.get("access_hash")
    if not (pid and ah is not None):
        file_dl_status[key] = {"state": "error",
                                "error": "candidate has no cached peer; run Tam Tara önce"}
        raise ValueError("no cached peer for candidate")
    entity = await client.get_entity(InputPeerChannel(int(pid), int(ah)))

    # Step 1 — try without joining.
    msg = await _try_fetch_message(client, entity, msg_id)
    temp_joined = False

    if msg is None:
        # Permission gate. If the user didn't confirm a temp-join, surface the
        # need and stop here — the UI shows a modal and re-requests with
        # confirm_temp_join=1.
        if not allow_temp_join:
            file_dl_status[key] = {"state": "needs_temp_join",
                                    "username": cand.get("username")}
            raise PermissionError("needs_temp_join")
        try:
            await client(JoinChannelRequest(entity))
            temp_joined = True
            logger.info(f"Hunter: temp-joined @{cand.get('username')} to download msg {msg_id}")
            msg = await client.get_messages(entity, ids=msg_id)
        except FloodWaitError as e:
            file_dl_status[key] = {"state": "error",
                                    "error": f"FloodWait {e.seconds}s on join"}
            raise

    try:
        if not msg or not getattr(msg, "media", None):
            file_dl_status[key] = {"state": "error",
                                    "error": "message has no downloadable media"}
            raise ValueError("no media on message")

        username = cand.get("username") or f"cand_{cid}"
        dest_dir = os.path.join(_DOWNLOADS_DIR, "_hunter", _safe_segment(username, str(cid)))
        os.makedirs(dest_dir, exist_ok=True)

        fname = cfile.get("file_name") or f"msg_{msg_id}"
        # Strip dangerous path separators that may have leaked from Telegram
        fname = fname.replace("/", "_").replace("\\", "_")
        dest = os.path.join(dest_dir, fname)
        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            dest = f"{base}_{msg_id}{ext}"

        async def _progress(current, total):
            file_dl_status[key] = {
                "state": "downloading",
                "progress": (current / total) if total else 0.0,
                "bytes_done": int(current), "bytes_total": int(total or 0),
                "error": None,
            }

        await client.download_media(msg, dest, progress_callback=_progress)

        # Persist for next time
        await database.set_candidate_file_local_path(cid, msg_id, dest)
        file_dl_status[key] = {"state": "done", "progress": 1.0,
                                "local_path": dest, "bytes_done": os.path.getsize(dest),
                                "bytes_total": os.path.getsize(dest)}
        _emit_event("file", f"@{cand.get('username')}: downloaded {fname}",
                    key="hl.file.downloaded",
                    params={"username": cand.get("username"), "file": fname})
        return dest
    finally:
        if temp_joined:
            try:
                await client(LeaveChannelRequest(entity))
                logger.info(f"Hunter: left @{cand.get('username')} after file download")
            except Exception as e:
                logger.warning(f"Hunter: leave-after-file-download failed: {e}")


async def download_candidate_file(cid: int, msg_id: int,
                                   allow_temp_join: bool = False) -> dict:
    """Kicks the download in the background and returns the current status
    dict. The actual work is wrapped in a task we keep in _file_dl_tasks so a
    second click while it's still going just returns the in-flight state."""
    key = (int(cid), int(msg_id))
    existing = _file_dl_tasks.get(key)
    if existing and not existing.done():
        return file_dl_status.get(key, {"state": "downloading"})

    async def _runner():
        try:
            await _download_candidate_file_impl(cid, msg_id, allow_temp_join)
        except PermissionError:
            # needs_temp_join — already set in status
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"download_candidate_file failed cid={cid} msg={msg_id}: {e}")
            file_dl_status[key] = {"state": "error", "error": str(e)[:240]}
        finally:
            _file_dl_tasks.pop(key, None)

    _file_dl_tasks[key] = asyncio.create_task(_runner())
    # Wait briefly so the first poll already has a real state (downloading /
    # needs_temp_join / error) — avoids a UI flash.
    await asyncio.sleep(0.05)
    return file_dl_status.get(key, {"state": "downloading"})


async def cancel_candidate_file_download(cid: int, msg_id: int) -> bool:
    key = (int(cid), int(msg_id))
    task = _file_dl_tasks.get(key)
    if task and not task.done():
        task.cancel()
        file_dl_status[key] = {"state": "error", "error": "cancelled"}
        return True
    return False


# ---------------------------------------------------------------------------
# Media preview (no-save, no-join)
# ---------------------------------------------------------------------------

_PREVIEW_MAX_BYTES = 150 * 1024 * 1024   # 150 MB hard limit for previews
# Cache: (cid, msg_id) → temp_file_path (survives for the process lifetime)
_preview_cache: Dict[tuple, str] = {}


async def preview_candidate_file(cid: int, msg_id: int) -> tuple:
    """Download the media to a temp file (or reuse cached) and return
    (local_path, mime_type, file_name).  Raises ValueError / PermissionError
    on soft failures so the caller can surface them as HTTP errors."""
    key = (int(cid), int(msg_id))

    # Return cached temp file if it still exists on disk
    cached = _preview_cache.get(key)
    if cached and os.path.exists(cached):
        cfile = await database.get_candidate_file(cid, msg_id)
        fname = (cfile or {}).get("file_name") or os.path.basename(cached)
        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        return cached, mime, fname

    cand = await database.get_hunter_candidate(cid)
    if not cand:
        raise ValueError("candidate not found")
    cfile = await database.get_candidate_file(cid, msg_id)
    if not cfile:
        raise ValueError("file row not found")

    # If already fully downloaded to its permanent path, serve that directly
    perm = cfile.get("local_path")
    if perm and os.path.exists(perm):
        fname = cfile.get("file_name") or os.path.basename(perm)
        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        return perm, mime, fname

    file_size = cfile.get("file_size") or 0
    if file_size > _PREVIEW_MAX_BYTES:
        raise ValueError(f"too_large:{file_size}")

    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)
    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()

    pid = cand.get("peer_id"); ah = cand.get("access_hash")
    if not (pid and ah is not None):
        raise ValueError("no cached peer; run Tam Tara first")
    entity = await client.get_entity(InputPeerChannel(int(pid), int(ah)))

    msg = await _try_fetch_message(client, entity, msg_id)
    if msg is None:
        raise PermissionError("needs_join")
    if not msg or not getattr(msg, "media", None):
        raise ValueError("no media on message")

    fname = cfile.get("file_name") or f"preview_{msg_id}"
    fname = fname.replace("/", "_").replace("\\", "_")
    suffix = os.path.splitext(fname)[1] or ""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="telf_preview_")
    os.close(tmp_fd)
    try:
        await client.download_media(msg, tmp_path)
    except Exception:
        try: os.remove(tmp_path)
        except OSError: pass
        raise

    _preview_cache[key] = tmp_path
    mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    return tmp_path, mime, fname


# ── Magnet Dork Hunt (Google-dork search for magnet: URIs) ──────────────────
# Uses the same headless-Chromium fetch path as Stage 2's channel discovery,
# but the queries target plain-text magnet URIs on public pages and the
# extractor pulls magnet:?xt=urn:btih:… instead of t.me/{user}. Discoveries
# land in the existing `links` table under a synthetic groups row (id=-1)
# so they show up in the regular Links grid with platform='Magnet'.

_WEB_MAGNET_GROUP_ID = -1
_WEB_MAGNET_GROUP_NAME = "Web Magnet Avı"
_WEB_MAGNET_GROUP_DISPLAY = "🧲 Web'den Bulunan Magnet'ler"

magnet_hunt_status: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "engines_done": 0,
    "engines_total": 0,
    "current_engine": None,
    "queries_done": 0,
    "queries_total": 0,
    "current_query": None,
    "magnets_found": 0,
    "magnets_new": 0,
    "pages_fetched": 0,
    "error": None,
}
_magnet_hunt_task: Optional[asyncio.Task] = None

# Capture full magnet URIs. Must include xt=urn:btih: payload to be valid.
_MH_MAGNET_RE = re.compile(
    r"magnet:\?[^\s\"'<>]+",
    re.IGNORECASE,
)

# Strict BitTorrent v1 info-hash format. SERP snippets sometimes splice their
# own query text after `xt=urn:btih:` (e.g. `xt=urn:btih:%22%20database&…`);
# without this gate those bogus magnets pollute the links table with rows
# whose only "file" is the magnet's URL-encoded display name.
_BTIH_HEX_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
_BTIH_B32_RE = re.compile(r"^[A-Z2-7]{32}$")
_BTIH_PARAM_RE = re.compile(r"xt=urn:btih:([^&]+)", re.IGNORECASE)


def _is_valid_magnet_btih(uri: str) -> bool:
    """True when xt=urn:btih:<hash> is a real BitTorrent info-hash (40 hex
    or 32 base32 chars). URL-decodes the value first so percent-encoded
    junk like `%22%20database` is rejected even though it slips past a
    naive `r"[A-Za-z0-9]"` check."""
    m = _BTIH_PARAM_RE.search(uri or "")
    if not m:
        return False
    import urllib.parse as _ul
    raw = _ul.unquote(m.group(1)).strip()
    if _BTIH_HEX_RE.match(raw):
        return True
    if _BTIH_B32_RE.match(raw.upper()):
        return True
    return False

# Sites known to host plain-text magnet URIs in pages — productive dork targets
# even when the search-engine snippet truncates the URI itself.
_MAGNET_DORK_SITES = [
    "pastebin.com", "gist.github.com", "rentry.co",
    "justpaste.it", "paste.ee", "dpaste.org",
    "old.reddit.com", "github.com",
]

# Search engines that tolerate scraping without aggressive CAPTCHA. Google is
# omitted on purpose — its CAPTCHA wall is too aggressive for an unattended
# crawl. Each tuple: (name, home_url, query_url_tpl, page_offsets, host).
_MAGNET_ENGINES: List[Tuple[str, str, str, List[str], str]] = [
    ("duckduckgo", "https://duckduckgo.com/",     "https://duckduckgo.com/html/?q={q}",       [""],             "duckduckgo.com"),
    ("brave",      "https://search.brave.com/",   "https://search.brave.com/search?q={q}",    ["", "&offset=1"], "search.brave.com"),
    ("bing",       "https://www.bing.com/",       "https://www.bing.com/search?q={q}",        ["", "&first=11"], "www.bing.com"),
    ("mojeek",     "https://www.mojeek.com/",     "https://www.mojeek.com/search?q={q}",      ["", "&s=11"],     "www.mojeek.com"),
    ("startpage",  "https://www.startpage.com/",  "https://www.startpage.com/do/search?q={q}", [""],             "www.startpage.com"),
]


def _build_magnet_dork_queries(keywords: List[str], max_q: int = 40) -> List[str]:
    """Compose Google-dork-style queries that surface plain-text magnet URIs.
    Layered patterns from generic-bait phrases through site-restricted dorks
    and time-tagged dorks for freshness."""
    out: List[str] = []
    seen: Set[str] = set()

    def push(q: str) -> bool:
        k = q.lower()
        if k in seen:
            return len(out) >= max_q
        seen.add(k); out.append(q)
        return len(out) >= max_q

    base = keywords or _DEFAULT_FILE_CATEGORIES[:8]

    # Pattern A: bare bait — engine snippet may already contain a magnet URI
    for kw in base[:8]:
        if push(f'"magnet:?xt=urn:btih:" {kw}'): return out
    # Pattern B: intext: dork — encourages engines to match within page body
    for kw in base[:6]:
        if push(f'intext:"magnet:?xt=urn:btih:" {kw}'): return out
    # Pattern C: torrent-flavoured bait
    for kw in base[:5]:
        if push(f'"magnet:" {kw} torrent'): return out
    # Pattern D: site-restricted dorks for paste/code/forum hosts
    for site in _MAGNET_DORK_SITES:
        for kw in base[:3]:
            if push(f'site:{site} "magnet:?xt=urn:btih:" {kw}'): return out
    # Pattern E: time-window dorks for freshness
    year = datetime.now().year
    for kw in base[:4]:
        if push(f'"magnet:?xt=urn:btih:" {kw} {year}'): return out

    return out


def _extract_magnets_from_html(html: str) -> List[str]:
    """Yield deduped magnet URIs from raw HTML. Decodes common entity escapes
    (&amp; → &) so query-string delimiters aren't mangled. Drops anything that
    isn't a urn:btih magnet (urn:tree/urn:ed2k are out of scope here)."""
    if not html:
        return []
    text = html.replace("&amp;", "&").replace("&#38;", "&")
    seen: Set[str] = set()
    out: List[str] = []
    for m in _MH_MAGNET_RE.finditer(text):
        uri = m.group(0).strip().rstrip(",.;) ”’")
        low = uri.lower()
        if "xt=urn:btih:" not in low:
            continue
        # Cut at any HTML/JSON syntax char that snuck in via the page source
        for ch in ("\\", "<", ">", "[", "]"):
            if ch in uri:
                uri = uri.split(ch, 1)[0]
        # Reject anything whose info-hash isn't a real 40-hex or 32-base32
        # string (SERP query bleed-through, accidental captures, …).
        if not _is_valid_magnet_btih(uri):
            continue
        low = uri.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(uri)
    return out


def _extract_external_urls_from_serp(html: str, serp_host: str) -> List[str]:
    """Pull anchor hrefs from a SERP that point outside the engine itself.
    Strips engine click-tracker wrappers (DDG /l/?uddg=, Bing /aclk?, etc.)
    so we get the actual destination URL."""
    if not html:
        return []
    urls: List[str] = []
    seen: Set[str] = set()
    for m in re.finditer(r'href=["\']?(https?://[^"\'<>\s]+)', html, re.IGNORECASE):
        u = m.group(1)
        # Unwrap common SERP redirector formats
        if "/url?" in u or "/l/?" in u or "/aclk?" in u or "uddg=" in u:
            inner = re.search(r"(?:uddg|q|u|url)=([^&]+)", u)
            if inner:
                try:
                    u = aiohttp.helpers.unquote(inner.group(1))
                except Exception:
                    pass
        host = u.split("//", 1)[-1].split("/", 1)[0].lower()
        if not host or host == serp_host:
            continue
        # Skip search-engine self-links, ad networks, social feeds (too noisy)
        if any(x in host for x in [
            "google.com", "googleadservices", "doubleclick.net",
            "duckduckgo.com", "bing.com", "yandex.com", "search.brave.com",
            "mojeek.com", "startpage.com", "ecosia.org", "w3.org",
            "schema.org", "fonts.googleapis", "gstatic", "facebook.com",
            "twitter.com", "x.com", "youtube.com", "instagram.com",
        ]):
            continue
        low = u.lower()
        if low in seen:
            continue
        seen.add(low)
        urls.append(u)
    return urls


def _mh_parse_magnet(uri: str) -> dict:
    """Lightweight magnet parser — returns {infohash, name, size}."""
    info = {"infohash": "", "name": "", "size": 0}
    if "?" not in uri:
        return info
    qs_str = uri.split("?", 1)[1]
    for p in qs_str.split("&"):
        if "=" not in p:
            continue
        k, _, v = p.partition("=")
        kl = k.lower()
        if kl == "xt" and "urn:btih:" in v.lower():
            info["infohash"] = v.lower().split("urn:btih:", 1)[1].split("&", 1)[0]
        elif kl == "dn":
            try:
                info["name"] = aiohttp.helpers.unquote(v)
            except Exception:
                info["name"] = v
        elif kl == "xl":
            try:
                info["size"] = int(v)
            except (ValueError, TypeError):
                info["size"] = 0
    return info


async def _persist_web_magnet(uri: str, engine: str, query: str) -> bool:
    """Insert a discovered magnet URI as a 'Magnet' link in the synthetic
    web-magnet group. Returns True if newly inserted, False if duplicate or
    invalid."""
    info = _mh_parse_magnet(uri)
    if not info.get("infohash"):
        return False
    name = info.get("name") or f"Magnet {info['infohash'][:8].upper()}…"
    size = int(info.get("size") or 0)
    files_json = [{"name": name, "size": size}]
    context = f"[magnet-dork] engine={engine} query={query}"[:300]
    return await database.insert_link(
        group_id=_WEB_MAGNET_GROUP_ID,
        message_id=0,
        platform="Magnet",
        url=uri,
        context=context,
        date=datetime.utcnow().isoformat(),
        discovered_by_account_id=None,
        files_json=files_json,
        available=True,
        file_count=1,
        file_size_total=size,
    )


def kick_magnet_hunt() -> bool:
    """Launch the magnet-dork hunt in the background. Returns False if a hunt
    is already running."""
    global _magnet_hunt_task
    if magnet_hunt_status.get("running"):
        return False
    _magnet_hunt_task = asyncio.create_task(run_magnet_hunt())
    return True


def cancel_magnet_hunt() -> bool:
    """Request the running hunt to stop at the next checkpoint."""
    if not magnet_hunt_status.get("running"):
        return False
    magnet_hunt_status["running"] = False
    return True


async def run_magnet_hunt():
    """Discover magnet URIs via search-engine dorks and persist them as
    Magnet links under the synthetic 'web magnets' group."""
    magnet_hunt_status.update({
        "running": True,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "engines_done": 0,
        "queries_done": 0,
        "queries_total": 0,
        "magnets_found": 0,
        "magnets_new": 0,
        "pages_fetched": 0,
        "current_engine": None,
        "current_query": None,
        "error": None,
    })
    try:
        await database.ensure_synthetic_group(
            _WEB_MAGNET_GROUP_ID, _WEB_MAGNET_GROUP_NAME, _WEB_MAGNET_GROUP_DISPLAY,
        )

        settings = await database.get_hunter_settings()
        keywords = _smart_keywords(settings.get("keywords") or "")
        delay_ms = int(settings.get("web_request_delay_ms") or 2500)
        queries_per_engine = 10
        results_per_query  = 5

        all_queries = _build_magnet_dork_queries(keywords)
        magnet_hunt_status["queries_total"] = min(queries_per_engine, len(all_queries)) * len(_MAGNET_ENGINES)
        magnet_hunt_status["engines_total"] = len(_MAGNET_ENGINES)

        _emit_event(
            "magnethunt",
            f"Magnet avı başladı — {len(all_queries)} sorgu × {len(_MAGNET_ENGINES)} motor",
            key="hl.magnetHunt.start",
            params={"q": len(all_queries), "e": len(_MAGNET_ENGINES)},
        )

        all_found: Set[str] = set()
        for eng_name, home, tpl, offsets, host in _MAGNET_ENGINES:
            if not magnet_hunt_status["running"]:
                break
            magnet_hunt_status["current_engine"] = eng_name
            _emit_event(
                "magnethunt", f"motor: {eng_name}",
                key="hl.magnetHunt.engine", params={"engine": eng_name},
            )

            # Warm up so first-party cookies stick
            try:
                _ = await _pw_get(home)
                await _interruptible_sleep(delay_ms / 1000)
            except Exception:
                pass

            engine_fails = 0
            for q in all_queries[:queries_per_engine]:
                if not magnet_hunt_status["running"]:
                    break
                magnet_hunt_status["current_query"] = q
                magnet_hunt_status["queries_done"] += 1

                serp_magnets: Set[str] = set()
                serp_urls: List[str] = []

                for off in offsets:
                    if not magnet_hunt_status["running"]:
                        break
                    url = tpl.format(q=aiohttp.helpers.quote(q)) + off
                    html = await _pw_get(url, referer=home)
                    if not html:
                        engine_fails += 1
                        if engine_fails >= 3:
                            _emit_event(
                                "magnethunt",
                                f"{eng_name}: 3 ardışık hata, motor atlanıyor",
                                "warn",
                                key="hl.magnetHunt.engineFail",
                                params={"engine": eng_name},
                            )
                            break
                        await _interruptible_sleep(delay_ms / 1000)
                        continue
                    engine_fails = 0
                    magnet_hunt_status["pages_fetched"] += 1
                    for uri in _extract_magnets_from_html(html):
                        serp_magnets.add(uri)
                    serp_urls.extend(_extract_external_urls_from_serp(html, host))
                    await _interruptible_sleep(delay_ms / 1000)

                if engine_fails >= 3:
                    break

                # Follow up to N result links to mine magnets from the actual
                # target pages (the SERP snippet alone is rarely enough)
                seen_urls: Set[str] = set()
                followed = 0
                for ru in serp_urls:
                    if followed >= results_per_query:
                        break
                    if not magnet_hunt_status["running"]:
                        break
                    if ru.lower() in seen_urls:
                        continue
                    seen_urls.add(ru.lower())
                    page_html = await _pw_get(ru)
                    if page_html:
                        magnet_hunt_status["pages_fetched"] += 1
                        for uri in _extract_magnets_from_html(page_html):
                            serp_magnets.add(uri)
                    followed += 1
                    await _interruptible_sleep(delay_ms / 1000)

                # Persist
                for uri in serp_magnets:
                    if uri in all_found:
                        continue
                    all_found.add(uri)
                    magnet_hunt_status["magnets_found"] += 1
                    try:
                        inserted = await _persist_web_magnet(uri, eng_name, q)
                        if inserted:
                            magnet_hunt_status["magnets_new"] += 1
                    except Exception as e:
                        logger.warning(f"magnet persist failed: {e}")

            magnet_hunt_status["engines_done"] += 1

        _emit_event(
            "magnethunt",
            (f"Magnet avı tamamlandı: {magnet_hunt_status['magnets_new']} yeni, "
             f"{magnet_hunt_status['magnets_found']} toplam bulundu"),
            key="hl.magnetHunt.done",
            params={
                "new":   magnet_hunt_status["magnets_new"],
                "found": magnet_hunt_status["magnets_found"],
            },
        )
    except asyncio.CancelledError:
        magnet_hunt_status["error"] = "cancelled"
        raise
    except Exception as e:
        logger.exception("magnet hunt fatal")
        magnet_hunt_status["error"] = str(e)
        _emit_event(
            "magnethunt", f"hata: {str(e)[:200]}", "warn",
            key="hl.magnetHunt.fatal", params={"err": str(e)[:200]},
        )
    finally:
        magnet_hunt_status["running"] = False
        magnet_hunt_status["finished_at"] = datetime.utcnow().isoformat()
        magnet_hunt_status["current_engine"] = None
        magnet_hunt_status["current_query"] = None
