import asyncio
import collections
import logging
import os
from datetime import datetime, timedelta, timezone
import time as _time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

import ui_auth

import database
import telegram_client
import torrent_parse as _torrent_parse
import transfer as _transfer
from sync import (
    cancel_download,
    download_file,
    kick_magnet_backfill,
    magnet_backfill_status,
    run_sync,
    setup_realtime_handler,
    sync_status,
    kick_archive_scan,
    cancel_archive_scan,
    archive_scan_status,
)
from telegram_client import (
    get_client,
    is_authorized,
    send_code,
    sign_in_with_code,
    sign_in_with_password,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-12s %(levelname)s %(message)s",
)
logger = logging.getLogger("main")

_log_buffer: collections.deque = collections.deque(maxlen=200)
_app_start = _time.time()
_status_cache: dict = {}
_next_sync_at: float = 0.0
_DEFAULT_SYNC_INTERVAL: int = 7200          # fallback if settings.json is missing/corrupt
_MIN_SYNC_INTERVAL: int = 900               # 15 minutes — below this floodwait is realistic
_MAX_SYNC_INTERVAL: int = 86400             # 24 hours — coarser than this defeats the purpose
_auto_sync_task: Optional[asyncio.Task] = None
_hunter_loop_task: Optional[asyncio.Task] = None
_link_probe_task: Optional[asyncio.Task] = None
_join_queue_task: Optional[asyncio.Task] = None
_bandwidth_checker_task: Optional[asyncio.Task] = None
_torrent_worker = _torrent_parse.TorrentParseWorker()

# ── App-level settings (persisted on the data volume) ─────────────────────────
import json as _json
_DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
_SETTINGS_PATH = os.path.join(_DATA_DIR, "settings.json")


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            return _json.load(f) or {}
    except Exception:
        return {}


def _save_settings(d: dict):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SETTINGS_PATH, "w") as f:
        _json.dump(d, f)


def get_sync_interval() -> int:
    raw = _load_settings().get("sync_interval_seconds")
    try:
        v = int(raw) if raw is not None else _DEFAULT_SYNC_INTERVAL
    except (TypeError, ValueError):
        v = _DEFAULT_SYNC_INTERVAL
    return max(_MIN_SYNC_INTERVAL, min(_MAX_SYNC_INTERVAL, v))


def set_sync_interval(seconds: int) -> int:
    seconds = max(_MIN_SYNC_INTERVAL, min(_MAX_SYNC_INTERVAL, int(seconds)))
    settings = _load_settings()
    settings["sync_interval_seconds"] = seconds
    _save_settings(settings)
    return seconds

class _DequeHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_buffer.append({
                "ts":    record.created,
                "level": record.levelname,
                "name":  record.name,
                "msg":   self.format(record)[:300],
            })
        except Exception:
            pass

_deque_handler = _DequeHandler()
_deque_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_deque_handler)


def _read_sys_info() -> dict:
    info: dict = {"uptime": _time.time() - _app_start}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    info["proc_rss_bytes"] = int(line.split()[1]) * 1024
    except Exception:
        pass
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.current") as f:
            info["cgroup_mem_used"] = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
            info["cgroup_mem_limit"] = None if raw == "max" else int(raw)
    except Exception:
        # cgroup v1 fallback
        try:
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                info["cgroup_mem_used"] = int(f.read().strip())
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
                limit = int(f.read().strip())
                info["cgroup_mem_limit"] = None if limit > (1 << 62) else limit
        except Exception:
            pass
    try:
        st = os.statvfs("/app/downloads")
        info["disk"] = {
            "total": st.f_blocks * st.f_frsize,
            "free":  st.f_bavail * st.f_frsize,
            "used":  (st.f_blocks - st.f_bfree) * st.f_frsize,
        }
    except Exception:
        pass
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            info["load"] = [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        pass
    return info


async def _auto_sync_loop():
    global _next_sync_at
    while True:
        # Re-read the interval each iteration so a runtime change via the
        # settings endpoint takes effect on the next wake without restart.
        interval = get_sync_interval()
        wait = max(1.0, _next_sync_at - _time.time())
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        if not sync_status["running"]:
            logger.info(f"Auto-sync: starting scheduled sync (interval={interval}s)")
            await run_sync()
        _next_sync_at = _time.time() + get_sync_interval()


async def _link_probe_loop():
    """Background worker: visit each saved link, record what files it serves
    (or that it's dead). Concurrency is intentionally low — we're scraping
    public file-hosts and don't want to burn provider goodwill."""
    import link_prober
    backoff = 5
    while True:
        try:
            batch = await database.get_links_due_for_probe(limit=20, stale_days=7)
            if not batch:
                await asyncio.sleep(120)
                backoff = 5
                continue
            sem = asyncio.Semaphore(3)
            async with link_prober.make_session() as session:
                async def _run(item):
                    async with sem:
                        res = await link_prober.probe_one(
                            session, item["platform"], item["url"]
                        )
                        await database.record_probe_result(
                            item["id"],
                            available=res["available"],
                            files=res["files"],
                            error=res["error"],
                        )
                await asyncio.gather(*[_run(it) for it in batch])
            await asyncio.sleep(2)
            backoff = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Link probe loop error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


async def _join_queue_loop():
    """Retry candidates whose Telegram join hit a FloodWait. Wakes every 30s,
    picks every queue entry whose due_at has passed, and re-runs join_candidate.

      success                    → delete from queue (status='joined' set inside)
      another FloodWait          → enqueue_join inside join_candidate updates due_at
      any other failure          → delete (don't retry forever for non-flood errors)
    """
    import hunter as _hunter
    while True:
        try:
            await asyncio.sleep(30)
            due = await database.list_due_joins(limit=20)
            for entry in due:
                cid = entry["candidate_id"]
                try:
                    result = await _hunter.join_candidate(cid)
                except Exception as e:
                    logger.warning(f"Join queue: candidate {cid} crashed: {e}")
                    await database.delete_join_from_queue(cid)
                    continue
                if result.get("queued"):
                    # Still rate-limited; the retry inside join_candidate
                    # already updated due_at via enqueue_join.
                    continue
                if result.get("ok"):
                    await database.delete_join_from_queue(cid)
                    logger.info(f"Join queue: candidate {cid} joined.")
                else:
                    err = result.get("error") or ""
                    if "flood" in err.lower():
                        # Belt + suspenders — enqueue if join_candidate took
                        # the legacy non-queued FloodWait path.
                        await database.enqueue_join(cid, entry["account_id"], 600, err)
                    else:
                        await database.delete_join_from_queue(cid)
                        logger.info(f"Join queue: candidate {cid} dropped, error: {err}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Join queue loop error: {e}")
            await asyncio.sleep(30)


async def _hunter_scheduler_loop():
    """Background loop: if hunter has schedule_kind='interval', kick a run
    when settings.next_run_at is due."""
    import hunter as _hunter
    while True:
        try:
            await asyncio.sleep(60)
            s = await database.get_hunter_settings()
            if not s.get("enabled"):
                continue
            if (s.get("schedule_kind") or "manual") != "interval":
                continue
            now = datetime.now(timezone.utc)
            nxt = s.get("next_run_at")
            # Older rows may be tz-naive; normalize before comparing.
            if nxt and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if nxt and nxt > now:
                continue
            if not _hunter.status["running"]:
                logger.info("Hunter: scheduled run starting")
                _hunter.kick_run()
                interval = max(3600, int(s.get("schedule_interval_seconds") or 86400))
                await database.update_hunter_settings({
                    "next_run_at": now + timedelta(seconds=interval)
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Hunter scheduler error: {e}")


# ── Bandwidth Scheduling ──────────────────────────────────────────────────────

def _time_in_range(start: str, end: str, current: str) -> bool:
    """True if current is within [start, end], handles overnight ranges like 22:00-06:00."""
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _schedule_allows_now(schedules: list, settings: dict) -> bool:
    """Returns True if downloads are allowed right now per the schedule."""
    if not settings.get("enabled"):
        return True
    now = datetime.now()
    weekday = now.weekday()   # 0=Mon … 6=Sun
    current_time = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    for s in schedules:
        if not s.get("enabled"):
            continue
        start = s.get("start_time", "02:00")
        end   = s.get("end_time",   "06:00")
        if s.get("rule_type", "weekly") == "weekly":
            if weekday in (s.get("days") or []) and _time_in_range(start, end, current_time):
                return True
        elif s.get("rule_type") == "specific_date":
            if s.get("specific_date") == today and _time_in_range(start, end, current_time):
                return True
    return False


def _minutes_until_next_window(schedules: list, settings: dict) -> Optional[int]:
    """Returns minutes until the next open window, or None if none found within 7 days."""
    if not settings.get("enabled"):
        return None
    active = [s for s in schedules if s.get("enabled")]
    if not active:
        return None
    now = datetime.now()
    for delta in range(5, 60 * 24 * 7, 5):
        t = now + timedelta(minutes=delta)
        t_str  = t.strftime("%H:%M")
        t_day  = t.weekday()
        t_date = t.strftime("%Y-%m-%d")
        for s in active:
            start = s.get("start_time", "02:00")
            end   = s.get("end_time",   "06:00")
            if s.get("rule_type", "weekly") == "weekly":
                if t_day in (s.get("days") or []) and _time_in_range(start, end, t_str):
                    return delta
            elif s.get("rule_type") == "specific_date":
                if s.get("specific_date") == t_date and _time_in_range(start, end, t_str):
                    return delta
    return None


async def _bandwidth_checker_loop():
    """Wake every 30 s; release explicitly-timed and bandwidth-window downloads."""
    while True:
        try:
            await asyncio.sleep(30)
            all_pending = await database.list_scheduled_downloads()
            if not all_pending:
                continue

            now_utc = datetime.now(timezone.utc)

            def _parse_dest_ids(entry):
                ids = entry.get("destination_ids") or []
                if isinstance(ids, str):
                    import json as _j2
                    try:
                        return _j2.loads(ids)
                    except Exception:
                        return []
                return ids

            # Release explicitly time-scheduled downloads regardless of bandwidth window
            for entry in all_pending:
                sat = entry.get("scheduled_at")
                if not sat:
                    continue
                if sat.tzinfo is None:
                    sat = sat.replace(tzinfo=timezone.utc)
                if sat <= now_utc:
                    fid = entry["file_id"]
                    dest_ids = _parse_dest_ids(entry)
                    await database.remove_scheduled_download(fid)
                    if dest_ids:
                        asyncio.create_task(_download_and_transfer(fid, dest_ids))
                    else:
                        asyncio.create_task(download_file(fid))
                    logger.info("Time-scheduled: started download for file_id=%s", fid)

            # Release bandwidth-window-deferred downloads (scheduled_at IS NULL)
            settings = await database.get_bandwidth_settings()
            if not settings.get("enabled"):
                continue
            schedules = await database.list_bandwidth_schedules()
            if not _schedule_allows_now(schedules, settings):
                continue
            for entry in all_pending:
                if entry.get("scheduled_at"):
                    continue
                fid = entry["file_id"]
                dest_ids = _parse_dest_ids(entry)
                await database.remove_scheduled_download(fid)
                if dest_ids:
                    asyncio.create_task(_download_and_transfer(fid, dest_ids))
                else:
                    asyncio.create_task(download_file(fid))
                logger.info("Bandwidth scheduler: started download for file_id=%s", fid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Bandwidth checker error: %s", e)


def _start_background_tasks():
    global _auto_sync_task, _next_sync_at, _hunter_loop_task, _link_probe_task, _join_queue_task, _bandwidth_checker_task
    asyncio.create_task(run_sync())
    if _auto_sync_task is None or _auto_sync_task.done():
        _next_sync_at = _time.time() + get_sync_interval()
        _auto_sync_task = asyncio.create_task(_auto_sync_loop())
    if _hunter_loop_task is None or _hunter_loop_task.done():
        _hunter_loop_task = asyncio.create_task(_hunter_scheduler_loop())
    # Always run the telemetry loop (it self-checks the enabled flag)
    try:
        import telemetry as _telemetry
        _telemetry.start_telemetry_loop()
    except Exception as e:
        logger.warning(f"telemetry loop start failed: {e}")
    if _link_probe_task is None or _link_probe_task.done():
        _link_probe_task = asyncio.create_task(_link_probe_loop())
    if _join_queue_task is None or _join_queue_task.done():
        _join_queue_task = asyncio.create_task(_join_queue_loop())
    if _bandwidth_checker_task is None or _bandwidth_checker_task.done():
        _bandwidth_checker_task = asyncio.create_task(_bandwidth_checker_loop())
    # Pre-warm the Files grid cache so the first tab-click after boot is
    # instant. The dedupe rows query takes ~2s cold; running it once at
    # startup populates the in-process cache. Fire-and-forget; if it fails
    # the user just pays the usual cold cost.
    asyncio.create_task(_prewarm_files_cache())
    # Periodic refresh of the files_canonical materialized view — keeps the
    # dedupe MV within ~60s of live state without blocking readers.
    asyncio.create_task(_files_canonical_refresh_loop())
    # Background embedder for semantic file search. No-op if pgvector or
    # sentence-transformers aren't available; logs and idles in that case.
    if getattr(database, "_VECTOR_AVAILABLE", False):
        try:
            import embed_worker
            embed_worker.start()
        except Exception as e:
            logger.warning(f"embed_worker start failed: {e}")


async def _files_canonical_refresh_loop():
    while True:
        try:
            await asyncio.sleep(60)
            await database.refresh_files_canonical(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"files_canonical refresh loop: {e}")


async def _prewarm_files_cache():
    try:
        await database.search_files(limit=100, offset=0, dedupe=True)
        logger.info("Files cache pre-warmed.")
    except Exception as e:
        logger.warning(f"Files cache pre-warm failed: {e}")


async def _attach_realtime(account_id: int):
    """Connect the account's client and register its realtime handler."""
    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        setup_realtime_handler(client, account_id)
        return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    asyncio.create_task(database.sync_torrent_files())

    stale = await database.reset_stale_downloads()
    if stale:
        logger.info(f"Reset {stale} stale download(s) left over from previous run.")

    # One-shot: queue old Mega links (probed before the decryption-aware
    # prober landed) for a re-fetch so their real filenames get recorded.
    try:
        mega_re = await database.reset_mega_probes_for_rescan()
        if mega_re:
            logger.info(f"Queued {mega_re} Mega link(s) for re-probe with filename decryption.")
    except Exception as e:
        logger.warning(f"Mega backfill prep failed: {e}")

    # Remove links from file hosts that have permanently shut down
    # (Zippyshare/Uploaded/Anonfiles/Bayfiles). Idempotent.
    try:
        dead = await database.delete_dead_platforms()
        if dead:
            logger.info(f"Deleted {dead} link(s) from defunct file hosts.")
    except Exception as e:
        logger.warning(f"Dead-platform cleanup failed: {e}")

    accounts = await database.list_accounts()
    if not accounts:
        logger.info("No accounts configured — open the web UI to add one.")
    else:
        any_authorized = False
        for a in accounts:
            try:
                client = await get_client(a["id"])
                await client.connect()
                if await client.is_user_authorized():
                    setup_realtime_handler(client, a["id"])
                    any_authorized = True
                    logger.info(f"[acc {a['id']}] {a['name']}: authorized.")
                else:
                    logger.info(f"[acc {a['id']}] {a['name']}: not authorized.")
            except Exception as e:
                logger.warning(f"[acc {a['id']}] init failed: {e}")
        if any_authorized:
            _start_background_tasks()
            logger.info("Background sync started.")

    yield

    await telegram_client.disconnect_all()
    logger.info("All Telegram clients disconnected.")


app = FastAPI(title="TelFiles", lifespan=lifespan)


# ── Web-UI password gate ─────────────────────────────────────────────────────

# Routes the gate must let through unauthenticated. Static files (the greeter
# itself, app.js, i18n.js, …) are served by StaticFiles which is mounted
# *after* this middleware sees the request — but those URLs don't match the
# /api/ prefix below, so they go through untouched.
_UI_AUTH_BYPASS = {
    "/api/uiauth/check",
    "/api/uiauth/login",
    "/api/uiauth/logout",
}


class _UIAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if path in _UI_AUTH_BYPASS:
            return await call_next(request)
        token = request.cookies.get(ui_auth.COOKIE_NAME)
        if not ui_auth.is_valid_token(token):
            return JSONResponse(
                {"detail": "Authentication required", "ui_auth": "required"},
                status_code=401,
            )
        return await call_next(request)


app.add_middleware(_UIAuthMiddleware)


class _UILoginReq(BaseModel):
    password: str
    remember: bool = False


class _UIChangePwReq(BaseModel):
    current_password: str
    new_password: str


@app.get("/api/uiauth/check")
async def ui_auth_check(request: Request):
    token = request.cookies.get(ui_auth.COOKIE_NAME)
    return {
        "authenticated": ui_auth.is_valid_token(token),
        "default_password": ui_auth.is_default_password(),
    }


@app.post("/api/uiauth/login")
async def ui_auth_login(req: _UILoginReq):
    if not ui_auth.verify_password(req.password):
        # 401 (not 400) so the frontend's auto-bounce-to-greeter logic
        # doesn't trigger on a wrong-password — the greeter is already on
        # screen and just shows the error inline.
        raise HTTPException(401, "Hatalı parola")
    token, ttl = ui_auth.create_token(bool(req.remember))
    resp = JSONResponse({
        "ok": True,
        "default_password": ui_auth.is_default_password(),
    })
    resp.set_cookie(
        ui_auth.COOKIE_NAME, token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/uiauth/logout")
async def ui_auth_logout(request: Request):
    ui_auth.revoke_token(request.cookies.get(ui_auth.COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(ui_auth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/uiauth/change-password")
async def ui_auth_change_password(req: _UIChangePwReq):
    if not ui_auth.verify_password(req.current_password):
        raise HTTPException(403, "Mevcut parola yanlış")
    if not (req.new_password or "").strip():
        raise HTTPException(400, "Yeni parola boş olamaz")
    ui_auth.set_password(req.new_password)
    return {"ok": True}


# ── Auth ──────────────────────────────────────────────────────────────────────

class PhoneRequest(BaseModel):
    phone: str
    account_id: Optional[int] = 1

class CodeRequest(BaseModel):
    phone: str
    code: str
    account_id: Optional[int] = 1

class PasswordRequest(BaseModel):
    password: str
    account_id: Optional[int] = 1


@app.get("/api/auth/status")
async def auth_status(account_id: int = Query(1)):
    """Returns authorization status for a specific account.
    Also returns aggregate "any_authorized" flag for backward compatibility."""
    accounts = await database.list_accounts()
    by_acc = {}
    any_auth = False
    for a in accounts:
        try:
            ok = await is_authorized(a["id"])
        except Exception:
            ok = False
        by_acc[a["id"]] = ok
        if ok:
            any_auth = True
    return {
        "authorized": by_acc.get(account_id, False),
        "any_authorized": any_auth,
        "accounts": [{"id": a["id"], "name": a["name"], "display_name": a.get("display_name"),
                       "phone": a.get("phone"), "authorized": by_acc.get(a["id"], False)} for a in accounts],
    }


@app.post("/api/auth/send-code")
async def auth_send_code(req: PhoneRequest):
    try:
        await send_code(req.account_id or 1, req.phone)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/verify-code")
async def auth_verify_code(req: CodeRequest):
    try:
        result = await sign_in_with_code(req.account_id or 1, req.phone, req.code)
        if result.get("needs_2fa"):
            return {"ok": True, "needs_2fa": True}
        await _post_auth(req.account_id or 1)
        return {"ok": True, "name": result.get("name")}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/verify-password")
async def auth_verify_password(req: PasswordRequest):
    try:
        result = await sign_in_with_password(req.account_id or 1, req.password)
        await _post_auth(req.account_id or 1)
        return {"ok": True, "name": result.get("name")}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/logout")
async def auth_logout(account_id: int = Query(1)):
    accounts = await database.list_accounts()
    if len(accounts) <= 1:
        # Last account: stop background tasks too
        await _stop_background_tasks()
    await telegram_client.logout(account_id)
    return {"ok": True}


async def _post_auth(account_id: int = 1):
    """Called after a fresh login completes. Hooks up realtime handler for
    this account and (re)starts global background tasks if needed."""
    try:
        await _attach_realtime(account_id)
    except Exception as e:
        logger.warning(f"[acc {account_id}] realtime hook failed: {e}")
    _start_background_tasks()


async def _stop_background_tasks():
    global _auto_sync_task
    if _auto_sync_task and not _auto_sync_task.done():
        _auto_sync_task.cancel()
        try:
            await _auto_sync_task
        except (asyncio.CancelledError, Exception):
            pass
    _auto_sync_task = None


# ── Credentials ───────────────────────────────────────────────────────────────

def _mask_hash(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "…" + s[-4:]


class CredentialsRequest(BaseModel):
    api_id: int
    api_hash: str
    account_id: Optional[int] = 1


@app.get("/api/credentials")
async def get_credentials(account_id: int = Query(1)):
    a = await database.get_account(account_id)
    if not a:
        return {"api_id": None, "api_hash": "", "api_hash_masked": "", "configured": False}
    h = a.get("api_hash") or ""
    return {
        "account_id": a["id"],
        "name": a.get("name"),
        "api_id": a.get("api_id"),
        "api_hash": h,
        "api_hash_masked": _mask_hash(h),
        "configured": bool(a.get("api_id") and h),
    }


@app.post("/api/credentials")
async def update_credentials(req: CredentialsRequest):
    if not req.api_id or not req.api_hash.strip():
        raise HTTPException(400, "API ID and API Hash are required")
    aid = req.account_id or 1
    a = await database.get_account(aid)
    if not a:
        # First-run case: .env was left blank during install, so init_db
        # didn't seed account 1. Create it now from the values the user
        # is entering through the greeter, so the login flow can proceed.
        if aid == 1:
            await database.create_account(
                "Hesap 1", int(req.api_id), req.api_hash.strip()
            )
            return {"ok": True, "next_step": "login", "created": True}
        raise HTTPException(404, f"Account {aid} not found")
    # Disconnect this account's client; if it's the only account, also stop sync loops
    accounts = await database.list_accounts()
    if len(accounts) <= 1:
        await _stop_background_tasks()
    await telegram_client.set_credentials(aid, req.api_id, req.api_hash.strip(), drop_session=True)
    return {"ok": True, "next_step": "login"}


# ── Accounts CRUD ─────────────────────────────────────────────────────────────

class AccountCreateRequest(BaseModel):
    name: str
    api_id: int
    api_hash: str

class AccountUpdateRequest(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    api_id: Optional[int] = None
    api_hash: Optional[str] = None


@app.get("/api/accounts")
async def list_accounts():
    rows = await database.list_accounts()
    out = []
    for r in rows:
        try:
            authed = await is_authorized(r["id"])
        except Exception:
            authed = False
        out.append({
            "id": r["id"],
            "name": r["name"],
            "display_name": r.get("display_name"),
            "phone": r.get("phone"),
            "is_active": r.get("is_active", True),
            "created_at": r.get("created_at"),
            "group_count": r.get("group_count", 0),
            "file_count": r.get("file_count", 0),
            "api_id": r.get("api_id"),
            "api_hash": r.get("api_hash") or "",
            "api_hash_masked": _mask_hash(r.get("api_hash") or ""),
            "authorized": authed,
        })
    return out


@app.post("/api/accounts")
async def create_account(req: AccountCreateRequest):
    if not req.name.strip() or not req.api_id or not req.api_hash.strip():
        raise HTTPException(400, "name, api_id, api_hash are required")
    aid = await database.create_account(req.name.strip(), int(req.api_id), req.api_hash.strip())
    return {"id": aid, "next_step": "login"}


@app.patch("/api/accounts/{account_id}")
async def update_account(account_id: int, req: AccountUpdateRequest):
    a = await database.get_account(account_id)
    if not a:
        raise HTTPException(404, "Account not found")
    await database.update_account(account_id, name=req.name, is_active=req.is_active,
                                   api_id=req.api_id, api_hash=req.api_hash or None)
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int):
    a = await database.get_account(account_id)
    if not a:
        raise HTTPException(404, "Account not found")
    # Disconnect/cleanup before DB delete
    try:
        await telegram_client.logout(account_id)
    except Exception:
        pass
    await database.delete_account(account_id)
    return {"ok": True}


# ── Settings (sync interval) ──────────────────────────────────────────────────

class SettingsRequest(BaseModel):
    sync_interval_seconds: int


@app.get("/api/settings")
async def get_settings():
    return {
        "sync_interval_seconds": get_sync_interval(),
        "min_sync_interval": _MIN_SYNC_INTERVAL,
        "max_sync_interval": _MAX_SYNC_INTERVAL,
        "next_sync_at": _next_sync_at,
    }


@app.put("/api/settings")
async def update_settings(req: SettingsRequest):
    global _next_sync_at
    applied = set_sync_interval(req.sync_interval_seconds)
    # Re-anchor the next-sync time so the change is felt immediately
    _next_sync_at = _time.time() + applied
    return {"ok": True, "sync_interval_seconds": applied, "next_sync_at": _next_sync_at}


# ── Sync ──────────────────────────────────────────────────────────────────────

@app.get("/api/sync/status")
async def get_sync_status():
    from sync import sync_status_by_account
    accounts = await database.list_accounts()
    by_acc = {}
    for a in accounts:
        s = sync_status_by_account.get(a["id"])
        if s is None:
            s = {"running": False, "current_group": None, "processed_groups": 0,
                 "total_groups": 0, "new_files": 0, "new_links": 0,
                 "last_sync_at": None, "error": None}
        by_acc[str(a["id"])] = {**s, "name": a["name"]}
    return {**sync_status, "next_sync_at": _next_sync_at, "by_account": by_acc}


@app.post("/api/sync/start")
async def start_sync(background_tasks: BackgroundTasks, account_id: Optional[int] = Query(None)):
    from sync import run_sync_account, sync_status_by_account
    if account_id is not None:
        s = sync_status_by_account.get(account_id)
        if s and s.get("running"):
            return {"ok": False, "message": f"Account {account_id} sync already running"}
        background_tasks.add_task(run_sync_account, account_id)
        return {"ok": True, "account_id": account_id}
    if sync_status["running"]:
        return {"ok": False, "message": "Sync already running"}
    background_tasks.add_task(run_sync)
    return {"ok": True}


# ── Groups ────────────────────────────────────────────────────────────────────

class GroupSettingsRequest(BaseModel):
    display_name: Optional[str] = None
    excluded: Optional[bool] = None
    hidden: Optional[bool] = None


@app.get("/api/groups")
async def list_groups(account_id: Optional[int] = Query(None)):
    if account_id is not None:
        return await database.get_groups_for_account(account_id)
    # No account specified: aggregate view across all accounts (legacy/unfiltered)
    return await database.get_groups()


@app.patch("/api/groups/{group_id}")
async def update_group(group_id: int, req: GroupSettingsRequest, account_id: Optional[int] = Query(None)):
    excluded_int = None if req.excluded is None else (1 if req.excluded else 0)
    hidden_int   = None if req.hidden   is None else (1 if req.hidden   else 0)
    if account_id is not None:
        await database.set_account_group_settings(account_id, group_id,
                                                   display_name=req.display_name,
                                                   excluded=excluded_int,
                                                   hidden=hidden_int)
    else:
        # Legacy: write to global groups table (kept for back-compat)
        await database.set_group_settings(group_id, req.display_name, excluded_int, hidden_int)
    return {"ok": True}


@app.post("/api/groups/{group_id}/rescan")
async def rescan_group(group_id: int, background_tasks: BackgroundTasks):
    """Reset this group's sync watermark and kick a fresh full sync.
    Existing rows are kept; insert_file's UniqueViolation dedups."""
    g = await database.get_group_by_id(group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    # Reset for ALL accounts that follow this group
    rows = await database._q("SELECT account_id FROM account_groups WHERE group_id = $1", group_id)
    if rows:
        for r in rows:
            await database.reset_account_group_watermark(r["account_id"], group_id)
    else:
        # Legacy fallback if no account_groups rows yet
        await database.reset_group_watermark(group_id)
    if not sync_status["running"]:
        background_tasks.add_task(run_sync)
    return {"ok": True, "group_id": group_id, "name": g.get("name"),
            "queued": not sync_status["running"]}


@app.post("/api/groups/{group_id}/leave")
async def leave_group(group_id: int, purge: bool = Query(False), account_id: int = Query(1)):
    """Leave the group/channel on Telegram. If purge=True, also drop the
    group's local rows (files, links, group). Otherwise mark excluded=True so
    future syncs skip it but its existing data stays browsable."""
    g = await database.get_group_by_id(group_id)
    if not g:
        raise HTTPException(404, "Group not found")

    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise HTTPException(403, "Telegram hesabı yetkili değil — hesabı yeniden doğrulayın")

    try:
        from telethon.errors import UserNotParticipantError
        entity = await client.get_entity(group_id)
        await client.delete_dialog(entity)
    except Exception as e:
        err_str = str(e)
        # "already not a member" is a success — the goal is achieved, just clean up DB.
        not_member = (
            "not a member" in err_str.lower()
            or "UserNotParticipant" in err_str
            or isinstance(e, UserNotParticipantError)
        )
        if not not_member:
            raise HTTPException(500, f"Telegram'dan ayrılınamadı: {e}")

    if purge:
        await database.delete_group_data(group_id)
    else:
        # Mark excluded for the leaving account; keep other accounts as-is
        await database.set_account_group_settings(account_id, group_id, excluded=1)
        # Also flip the global flag
        await database.set_group_settings(group_id, None, 1, None)

    logger.info(f"Left Telegram dialog: {g.get('name')} ({group_id}), purge={purge}")
    return {"ok": True, "group_id": group_id, "name": g.get("name"), "purged": purge}


# ── Channels: per-channel detail (hunter-shaped, used by the Channels grid) ──

class SimilarChannelsRequest(BaseModel):
    """Right-click "Show similar channels" lookup. Caller passes ONE of:
      • username (raw or @prefixed)
      • group_id (a followed group — we'll pull its cached peer/access)
      • candidate_id (a hunter_candidate — same)"""
    username: Optional[str] = None
    group_id: Optional[int] = None
    candidate_id: Optional[int] = None
    files_limit: Optional[int] = 30
    max_recommendations: Optional[int] = 12


@app.post("/api/channels/similar")
async def channels_similar(req: SimilarChannelsRequest):
    """Live "similar channels" preview backing the right-click lightbox in
    the Channels and Hunter grids. Returns the Telegram-recommended channels
    for the seed plus a sample of each one's recent files (name + size + ext
    + date). Driven by `channels.getChannelRecommendations` + a light
    `iter_messages` pass per result."""
    import hunter as _hunter

    seed_username = (req.username or "").strip().lstrip("@") or None
    seed_pid = None
    seed_ah = None
    seed_gid = None

    if req.group_id is not None:
        g = await database.get_group_by_id(req.group_id)
        if g:
            # `groups.id` IS the channel ID. Pass it down so Telethon's session
            # cache (which has access_hash for everything we follow) can
            # resolve the entity WITHOUT a username — required for private
            # channels with no public @-handle.
            seed_gid = int(req.group_id)
            seed_username = seed_username or g.get("username")
    elif req.candidate_id is not None:
        cand = await database._qrow(
            "SELECT username, peer_id, access_hash FROM hunter_candidates WHERE id = $1",
            req.candidate_id,
        )
        if cand:
            seed_username = seed_username or cand["username"]
            seed_pid = cand["peer_id"]
            seed_ah = cand["access_hash"]

    if not seed_username and seed_pid is None and seed_gid is None:
        raise HTTPException(400, "username, group_id or candidate_id required")

    try:
        data = await _hunter.get_similar_channels_preview(
            seed_username=seed_username,
            seed_peer_id=seed_pid,
            seed_access_hash=seed_ah,
            seed_group_id=seed_gid,
            files_limit=max(0, min(100, int(req.files_limit or 30))),
            max_recommendations=max(1, min(30, int(req.max_recommendations or 12))),
        )
    except Exception as e:
        raise HTTPException(500, f"similar lookup failed: {e}")
    return data


@app.get("/api/channels/{group_id}/detail")
async def channels_detail(group_id: int):
    """Return a hunter-candidate-shaped JSON for an already-joined group so the
    Channels tab can reuse the same detail popup as the Channel Hunter. If the
    group's username matches an existing hunter_candidate, overlay its richer
    fields (score, sampled messages, sources from external discovery) on top
    of the group's own counts."""
    g = await database.get_group_by_id(group_id)
    if not g:
        raise HTTPException(404, "Group not found")

    rows = await database._q(
        """SELECT
              COUNT(*)::bigint                                            AS file_count,
              COALESCE(SUM(file_size), 0)::bigint                         AS total_size,
              MAX(date)                                                   AS last_message_at,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('mp3','flac','wav','aac','ogg','m4a','opus','wma','ape','alac','mid','midi')) AS type_audio,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('mp4','mkv','avi','mov','wmv','flv','webm','m4v','ts','vob','rm','rmvb','3gp')) AS type_video,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('jpg','jpeg','png','gif','bmp','webp','svg','tiff','tif','heic','ico','raw')) AS type_image,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('zip','rar','7z','tar','gz','bz2','xz','zst','cab','ace','lzh','lz4','iso')) AS type_archive,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('pdf','doc','docx','xls','xlsx','ppt','pptx','odt','ods','odp','txt','epub','rtf','csv','md')) AS type_document,
              COUNT(*) FILTER (WHERE LOWER(file_ext) IN ('exe','apk','dmg','deb','rpm','msi','pkg','bin','jar','sh','bat','ps1')) AS type_software,
              COUNT(*) FILTER (WHERE LOWER(file_ext) = 'torrent') AS type_torrent
           FROM files WHERE group_id = $1""",
        group_id,
    )
    f = dict(rows[0]) if rows else {}
    file_count = int(f.get("file_count") or 0)
    total_size = int(f.get("total_size") or 0)
    breakdown = {
        "audio":    int(f.get("type_audio")    or 0),
        "video":    int(f.get("type_video")    or 0),
        "image":    int(f.get("type_image")    or 0),
        "archive":  int(f.get("type_archive")  or 0),
        "document": int(f.get("type_document") or 0),
        "software": int(f.get("type_software") or 0),
        "torrent":  int(f.get("type_torrent")  or 0),
    }
    accounted = sum(breakdown.values())
    breakdown["other"] = max(0, file_count - accounted)

    # If the channel was previously seen by the hunter, surface its discovery
    # metadata + sources alongside the live group stats.
    cand = None
    if g.get("username"):
        cand = await database.get_hunter_candidate_by_username(g["username"])

    sources = (cand.get("sources") if cand else None) or ["internal:owned"]

    return {
        "kind":                "channel",   # discriminator for the frontend
        "group_id":            group_id,
        "id":                  cand.get("id") if cand else None,
        "title":               g.get("display_name") or g.get("name"),
        "username":            g.get("username") or "",
        "description":         (cand or {}).get("description"),
        "members":             g.get("member_count"),
        "score":               (cand or {}).get("score") or 0,
        "file_count_sample":   file_count,
        "sampled_messages":    file_count,
        "avg_file_size":       int(total_size / file_count) if file_count else 0,
        "total_size":          total_size,
        "last_message_at":     f.get("last_message_at"),
        "discovered_at":       (cand or {}).get("discovered_at") or g.get("last_synced_at"),
        "last_synced_at":      g.get("last_synced_at"),
        "sources":             sources,
        "status":              "joined",
        "hidden":              bool(g.get("hidden")),
        "excluded":            bool(g.get("excluded")),
        "file_type_breakdown": breakdown,
        "error":               None,
    }


@app.get("/api/channels/{group_id}/files")
async def channel_files_list(
    group_id: int,
    q: str = Query(""),
    ext: str = Query(""),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    limit: int = Query(500, le=2000),
    offset: int = Query(0),
):
    files, total = await database.list_channel_files(
        group_id, q=q, ext=ext, sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset,
    )
    summary = await database.channel_file_summary(group_id)
    return {"files": files, "total": total, "summary": summary,
            "limit": limit, "offset": offset}


# ── Channels: bulk add / parse from free-form text ────────────────────────────

import re as _re
import aiohttp

_CHANNEL_URL_RE = _re.compile(
    r"(?:@|t(?:elegram)?\.me/|tg://resolve\?domain=)([A-Za-z][A-Za-z0-9_]{3,31})\b"
)
_CHANNEL_BARE_RE = _re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_URL_RE = _re.compile(r"https?://[^\s<>\"\)\]]+", _re.IGNORECASE)
_HREF_RE = _re.compile(r"""href=["']([^"']+)["']""", _re.IGNORECASE)
_TAG_RE  = _re.compile(r"<[^>]+>")


def _extract_channel_usernames(text: str) -> list[str]:
    """Pull every plausible Telegram channel username out of arbitrary text.

    Strategy:
      1. Always extract anything explicitly marked: @username, t.me/x,
         telegram.me/x, tg://resolve?domain=x.
      2. ONLY if no marked usernames were found, fall back to bare-token
         matching (pure plaintext list with no @ prefixes). This avoids
         false positives like picking up English/Turkish words from
         descriptions or ASCII tables — e.g. "politik" in a "Tahmini
         içerik" column shouldn't become @politik just because the same
         paste contains other @-prefixed names."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _CHANNEL_URL_RE.finditer(text):
        u = m.group(1).lower()
        if u not in seen:
            seen.add(u); out.append(u)
    if out:
        return out
    # Fallback: bare-token list (every token on its own must be a username).
    for tok in _re.split(r"[\s,;]+", text):
        m = _CHANNEL_BARE_RE.match(tok.strip())
        if m:
            u = m.group(1).lower()
            if u not in seen:
                seen.add(u); out.append(u)
    return out


def _is_telegram_url(url: str) -> bool:
    u = url.lower()
    return ("t.me/" in u) or ("telegram.me/" in u) or u.startswith("tg://")


async def _expand_external_urls(text: str,
                                 fetch_timeout: float = 8.0,
                                 max_urls: int = 8) -> str:
    """For each non-Telegram URL in `text`, fetch the body and append its
    href= attributes + tag-stripped text so the username regex picks up
    channels that only appear inside the linked page. Telegram URLs are
    left untouched (they're already pattern-matched directly)."""
    if not text:
        return text
    urls = [u for u in _URL_RE.findall(text) if not _is_telegram_url(u)]
    # Dedupe while preserving order, then cap so a malicious paste cannot
    # exhaust the fetcher.
    seen: set[str] = set(); ordered = []
    for u in urls:
        if u in seen: continue
        seen.add(u); ordered.append(u)
        if len(ordered) >= max_urls: break
    if not ordered:
        return text

    headers = {"User-Agent": "telfiles/0.4 (channel-add fetcher)"}
    timeout = aiohttp.ClientTimeout(total=fetch_timeout)

    async def fetch_one(url: str) -> str:
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(url, allow_redirects=True) as r:
                    if r.status != 200:
                        return ""
                    raw = await r.text(errors="ignore")
        except Exception:
            return ""
        hrefs = " ".join(_HREF_RE.findall(raw))
        body  = _TAG_RE.sub(" ", raw)
        return hrefs + " " + body

    pieces = await asyncio.gather(*(fetch_one(u) for u in ordered))
    return "\n".join([text, *pieces])


class ChannelAddRequest(BaseModel):
    text: Optional[str] = None
    usernames: Optional[list[str]] = None  # Pre-parsed list — skips text parsing.
    action: str = "join"                   # "join" → subscribe; "hunter" → queue
    fetch_urls: bool = True


class ChannelParseRequest(BaseModel):
    text: str
    fetch_urls: bool = True


@app.post("/api/channels/parse")
async def channels_parse(req: ChannelParseRequest):
    """Resolve a free-form blob (including external page URLs) into a deduped
    Telegram channel username list. Frontend uses this for the 'Çözümle'
    preview before deciding which action to dispatch."""
    text = req.text or ""
    if req.fetch_urls:
        text = await _expand_external_urls(text)
    usernames = _extract_channel_usernames(text)
    return {"ok": True, "usernames": usernames}


@app.post("/api/channels/add")
async def channels_add(req: ChannelAddRequest):
    if req.usernames is not None:
        # Caller supplied an already-parsed list (chunked submit path).
        usernames: list[str] = []
        seen: set[str] = set()
        for raw in req.usernames:
            u = (raw or "").strip().lstrip("@").lower()
            if u and u not in seen:
                seen.add(u); usernames.append(u)
    else:
        text = req.text or ""
        if req.fetch_urls:
            text = await _expand_external_urls(text)
        usernames = _extract_channel_usernames(text)

    if not usernames:
        return {"ok": True, "parsed": [], "joined": [], "queued": [],
                "skipped": [], "failed": [], "added": []}

    if req.action == "hunter":
        existing = await database.check_hunter_candidates_bulk(usernames)
        added: list[str] = []
        skipped_blacklisted: list[str] = []
        skipped_joined: list[str] = []
        skipped_queued: list[str] = []
        for u in usernames:
            ex = existing.get(u)
            if ex == "blacklisted":
                skipped_blacklisted.append(u)
            elif ex == "joined":
                skipped_joined.append(u)
            elif ex is not None:   # queued / enriched / rejected / failed
                skipped_queued.append(u)
            else:
                cid = await database.upsert_hunter_candidate(u)
                if cid:
                    await database.add_hunter_source(cid, "manual:paste", None)
                    added.append(u)
        return {
            "ok": True, "parsed": usernames, "added": added,
            "skipped_blacklisted": skipped_blacklisted,
            "skipped_joined": skipped_joined,
            "skipped_queued": skipped_queued,
        }

    # Default: directly join. Each username goes through the hunter join path
    # so peer caching, FloodWait handling and the join queue all work for free.
    import hunter as _hunter
    joined: list[dict] = []
    queued: list[dict] = []
    skipped: list[dict] = []
    failed:  list[dict] = []
    for u in usernames:
        cid = await database.upsert_hunter_candidate(u)
        if not cid:
            failed.append({"username": u, "error": "could not upsert"})
            continue
        await database.add_hunter_source(cid, "manual:paste", None)
        try:
            r = await _hunter.join_candidate(cid)
        except Exception as e:
            failed.append({"username": u, "error": str(e)})
            continue
        if r.get("ok"):
            if r.get("already_member"):
                skipped.append({"username": u, "group_id": r.get("group_id")})
            elif r.get("queued"):
                queued.append({"username": u, "wait_s": r.get("wait_s")})
            else:
                joined.append({"username": u, "group_id": r.get("group_id")})
        else:
            failed.append({"username": u, "error": r.get("error") or "unknown"})

    return {"ok": True, "parsed": usernames,
            "joined": joined, "queued": queued,
            "skipped": skipped, "failed": failed}


# ── Files ─────────────────────────────────────────────────────────────────────

@app.get("/api/files")
async def search_files(
    q: str = Query(""),
    ext: str = Query(""),
    ext_group: str = Query(""),
    group_id: Optional[int] = Query(None),
    group_ids: Optional[str] = Query(None),
    file_ids: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    size_min: Optional[int] = Query(None),
    size_max: Optional[int] = Query(None),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
    dedupe: bool = Query(True),
    mode: str = Query("exact"),
    search_caption: bool = Query(False),
):
    gids: Optional[list] = None
    if group_ids:
        try:
            gids = [int(x) for x in group_ids.split(",") if x.strip()]
        except ValueError:
            gids = None
    fids: Optional[list] = None
    if file_ids:
        try:
            fids = [int(x) for x in file_ids.split(",") if x.strip()]
        except ValueError:
            fids = None
    files, stats = await database.search_files(
        query=q, ext=ext, ext_group=ext_group, group_id=group_id, group_ids=gids, file_ids=fids,
        date_from=date_from, date_to=date_to,
        size_min=size_min, size_max=size_max,
        sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset,
        dedupe=dedupe,
        mode=mode if mode in ("exact", "semantic", "hybrid") else "exact",
        search_caption=search_caption,
    )
    return {
        "files": files,
        "total":         stats.get("total", 0),
        "virtual_total": stats.get("virtual_total", 0),
        "total_size":    stats.get("total_size", 0),
        "limit": limit, "offset": offset,
        "mode": mode,
    }


@app.get("/api/embed/status")
async def embed_status():
    """Reports whether semantic search is available and how far the
    background embedder has progressed."""
    available = getattr(database, "_VECTOR_AVAILABLE", False)
    progress = {}
    if available:
        try:
            import embed_worker
            progress = embed_worker.get_progress()
        except Exception:
            pass
    return {"available": available, **progress}


@app.get("/api/link-files")
async def search_link_files(
    q: str = Query(""),
    limit: int = Query(200, le=500),
    offset: int = Query(0),
):
    """Harici link kayıtlarının files_json içinde dosya adına göre arama.
    Sonuçlar Dosyalar tabında Telegram dosyalarıyla birlikte gösterilir."""
    if not q:
        return {"files": []}
    rows = await database.search_link_files(q, limit, offset)
    return {"files": rows}


@app.get("/api/files/shares")
async def file_shares(fname: str = Query(...), fsize: int = Query(...)):
    """Return the channels in which the (file_name, file_size) pair appears.
    Used by the Files-grid dup badge tooltip to expand "×3" into the actual
    channel list on hover. Capped at 30 rows so the tooltip stays readable."""
    rows = await database._q(
        """SELECT f.group_id, f.message_id, f.date,
                  COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
             FROM files f
             JOIN groups g ON g.id = f.group_id
            WHERE f.file_name = $1 AND f.file_size = $2
         ORDER BY f.date DESC
            LIMIT 30""",
        fname, fsize,
    )
    return {"shares": [dict(r) for r in rows], "limited_to": 30}


@app.get("/api/files/top-shared")
async def top_shared_files(
    window: str = Query("all"),   # 'all' | '7d' | '30d'
    limit:  int = Query(30, le=100),
    min_shares: int = Query(2, ge=1),
):
    items = await database.get_top_shared_files(window=window, limit=limit, min_shares=min_shares)
    return {"items": items, "window": window, "limit": limit}


@app.get("/api/files/{file_id}")
async def get_file(file_id: int):
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    return f


class DownloadRequest(BaseModel):
    destination_ids: list[int] = []
    scheduled_at: Optional[str] = None


async def _download_and_transfer(file_id: int, destination_ids: list[int]):
    local_path = await download_file(file_id)
    if not destination_ids:
        return
    dests = await database.list_transfer_destinations()
    dests_by_id = {d["id"]: d for d in dests}
    for dest_id in destination_ids:
        dest = dests_by_id.get(dest_id)
        if not dest or not dest.get("enabled"):
            continue
        try:
            await _transfer.transfer_file(local_path, dest)
            logger.info("Transfer tamamlandı: file_id=%s dest=%s", file_id, dest["name"])
        except Exception as exc:
            logger.error("Transfer hatası: file_id=%s dest=%s hata=%s", file_id, dest["name"], exc)


@app.post("/api/files/{file_id}/download")
async def trigger_download(file_id: int, background_tasks: BackgroundTasks, body: DownloadRequest = DownloadRequest()):
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")

    if f["local_path"] and os.path.exists(f["local_path"]):
        if body.destination_ids:
            # Dosya zaten indirdi ama transfer istendi — download_file yerel cache'ten döner
            background_tasks.add_task(_download_and_transfer, file_id, body.destination_ids)
            return {"status": "transfer_started", "path": f["local_path"]}
        return {"status": "already_downloaded", "path": f["local_path"]}

    if f["downloading"]:
        return {"status": "downloading", "progress": f["download_progress"]}

    # Explicit user-chosen schedule time
    if body.scheduled_at:
        try:
            dt = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            await database.add_scheduled_download(file_id, body.destination_ids or [], scheduled_at=dt)
            return {"status": "scheduled"}
        except Exception as e:
            logger.warning("Bad scheduled_at value: %s — %s", body.scheduled_at, e)

    # Check bandwidth scheduling
    bw_settings = await database.get_bandwidth_settings()
    if bw_settings.get("enabled"):
        min_mb = bw_settings.get("min_size_mb", 0)
        file_size = f.get("file_size") or 0
        if min_mb == 0 or file_size >= min_mb * 1024 * 1024:
            bw_schedules = await database.list_bandwidth_schedules()
            if not _schedule_allows_now(bw_schedules, bw_settings):
                await database.add_scheduled_download(file_id, body.destination_ids or [])
                return {"status": "scheduled"}

    if body.destination_ids:
        background_tasks.add_task(_download_and_transfer, file_id, body.destination_ids)
    else:
        background_tasks.add_task(download_file, file_id)
    return {"status": "started"}


@app.get("/api/files/{file_id}/blob")
async def stream_file_blob(file_id: int):
    f = await database.get_file_by_id(file_id)
    if not f or not f["local_path"]:
        raise HTTPException(404, "File not downloaded yet")
    if not os.path.exists(f["local_path"]):
        # Disk file disappeared — clean up the DB pointer so the UI reflects reality
        await database.clear_file_local_path(file_id)
        raise HTTPException(404, "File missing on disk")
    return FileResponse(
        f["local_path"],
        media_type=f.get("mime_type") or "application/octet-stream",
        filename=f.get("file_name") or f"file_{file_id}",
    )


@app.post("/api/files/{file_id}/cancel")
async def cancel_file_download(file_id: int):
    if cancel_download(file_id):
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False, "message": "No active download"}


@app.post("/api/files/{file_id}/forward-to-me")
async def forward_file_to_me(file_id: int):
    """Server-side forward of the source Telegram message into the user's own
    Saved Messages chat. Doesn't download the file — uses the file's stored
    (group_id, message_id) and the account that originally discovered it.

    Two efficiency wins vs. download-then-upload:
      • Zero bandwidth: forward stays inside Telegram's network
      • Instant: no MTProto chunk upload required (4 GB files become 1 RPC)
    """
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    gid    = f.get("group_id")
    msg_id = f.get("message_id")
    acc_id = int(f.get("discovered_by_account_id") or 1)
    if gid is None or msg_id is None:
        raise HTTPException(400, "File has no source message to forward")
    try:
        client = await get_client(acc_id)
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "Telegram not authorized for that account")
        # `"me"` resolves to the user's own Saved Messages peer.
        await client.forward_messages("me", int(msg_id), from_peer=int(gid))
    except HTTPException:
        raise
    except Exception as e:
        # Capture the underlying class so the UI can show a useful tooltip
        # rather than a generic 500.
        logger.warning(f"forward-to-me failed (file {file_id}): {e}")
        raise HTTPException(500, f"Forward failed: {e}")
    return {"ok": True, "file_id": file_id}


@app.delete("/api/files/{file_id}/local")
async def delete_local_file(file_id: int):
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    path = f.get("local_path")
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            raise HTTPException(500, f"Could not delete file: {e}")
    await database.clear_file_local_path(file_id)
    return {"ok": True}


# ── Transfer Destinations ─────────────────────────────────────────────────────

class TransferDestBody(BaseModel):
    name: str
    type: str
    config: dict = {}
    enabled: bool = True


@app.get("/api/transfer-destinations")
async def api_list_transfer_destinations():
    return await database.list_transfer_destinations()


@app.post("/api/transfer-destinations")
async def api_create_transfer_destination(body: TransferDestBody):
    allowed = {"local", "ftp", "sftp"}
    if body.type not in allowed:
        raise HTTPException(400, f"Geçersiz tür. İzin verilenler: {', '.join(allowed)}")
    dest = await database.create_transfer_destination(body.name, body.type, body.config, body.enabled)
    return dest


@app.put("/api/transfer-destinations/{dest_id}")
async def api_update_transfer_destination(dest_id: int, body: TransferDestBody):
    allowed = {"local", "ftp", "sftp"}
    if body.type not in allowed:
        raise HTTPException(400, f"Geçersiz tür. İzin verilenler: {', '.join(allowed)}")
    dest = await database.update_transfer_destination(dest_id, body.name, body.type, body.config, body.enabled)
    if not dest:
        raise HTTPException(404, "Hedef bulunamadı")
    return dest


@app.delete("/api/transfer-destinations/{dest_id}")
async def api_delete_transfer_destination(dest_id: int):
    ok = await database.delete_transfer_destination(dest_id)
    if not ok:
        raise HTTPException(404, "Hedef bulunamadı")
    return {"ok": True}


@app.post("/api/transfer-destinations/{dest_id}/test")
async def api_test_transfer_destination(dest_id: int):
    dest = await database.get_transfer_destination(dest_id)
    if not dest:
        raise HTTPException(404, "Hedef bulunamadı")
    result = await _transfer.test_destination(dest)
    return result


class TransferTestConfigBody(BaseModel):
    type: str
    config: dict = {}


@app.post("/api/transfer-destinations/test-config")
async def api_test_transfer_config(body: TransferTestConfigBody):
    """Kaydetmeden önce geçici bir yapılandırmayı test et."""
    dest = {"type": body.type, "config": body.config}
    result = await _transfer.test_destination(dest)
    return result


@app.get("/api/downloads")
async def list_downloads():
    return await database.list_downloaded_files()


@app.get("/api/downloads/active")
async def list_active_downloads():
    return await database.list_downloading_files()


@app.get("/api/downloads/scheduled")
async def list_scheduled_downloads_endpoint():
    return await database.list_scheduled_downloads()


@app.delete("/api/downloads/scheduled/{file_id}")
async def cancel_scheduled_download(file_id: int):
    await database.remove_scheduled_download(file_id)
    return {"ok": True}


# ── Bandwidth Scheduling ───────────────────────────────────────────────────────

class BandwidthSettingsBody(BaseModel):
    enabled: bool = False
    min_size_mb: int = 0


class BandwidthScheduleBody(BaseModel):
    name: str
    enabled: bool = True
    rule_type: str = "weekly"
    days: list[int] = []
    start_time: str = "02:00"
    end_time: str = "06:00"
    specific_date: Optional[str] = None


@app.get("/api/bandwidth/settings")
async def get_bw_settings():
    return await database.get_bandwidth_settings()


@app.put("/api/bandwidth/settings")
async def set_bw_settings(body: BandwidthSettingsBody):
    await database.set_bandwidth_settings(body.enabled, body.min_size_mb)
    if not body.enabled:
        # Scheduling disabled — release bandwidth-deferred downloads (not explicit-time ones)
        pending = await database.list_scheduled_downloads()
        for entry in pending:
            if entry.get("scheduled_at"):
                continue  # explicit-time schedule; keep it
            fid      = entry["file_id"]
            dest_ids = entry.get("destination_ids") or []
            await database.remove_scheduled_download(fid)
            if dest_ids:
                asyncio.create_task(_download_and_transfer(fid, dest_ids))
            else:
                asyncio.create_task(download_file(fid))
    return {"ok": True}


@app.get("/api/bandwidth/schedules")
async def list_bw_schedules():
    return await database.list_bandwidth_schedules()


@app.post("/api/bandwidth/schedules")
async def create_bw_schedule(body: BandwidthScheduleBody):
    return await database.create_bandwidth_schedule(body.model_dump())


@app.put("/api/bandwidth/schedules/{schedule_id}")
async def update_bw_schedule(schedule_id: int, body: BandwidthScheduleBody):
    result = await database.update_bandwidth_schedule(schedule_id, body.model_dump())
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@app.delete("/api/bandwidth/schedules/{schedule_id}")
async def delete_bw_schedule(schedule_id: int):
    await database.delete_bandwidth_schedule(schedule_id)
    return {"ok": True}


@app.get("/api/bandwidth/status")
async def get_bw_status():
    settings  = await database.get_bandwidth_settings()
    schedules = await database.list_bandwidth_schedules()
    allowed   = _schedule_allows_now(schedules, settings)
    now       = datetime.now()
    minutes   = _minutes_until_next_window(schedules, settings) if not allowed else None
    return {
        "enabled":         settings.get("enabled", False),
        "allowed":         allowed,
        "current_time":    now.strftime("%H:%M:%S"),
        "current_day":     now.weekday(),
        "min_size_mb":     settings.get("min_size_mb", 0),
        "scheduled_count": await database.count_scheduled_downloads(),
        "minutes_until_next": minutes,
    }


# ── Links ─────────────────────────────────────────────────────────────────────

@app.get("/api/links")
async def search_links_endpoint(
    q: str = Query(""),
    platform: str = Query(""),
    group_id: Optional[int] = Query(None),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
    show_dead: bool = Query(False),
    dedupe: bool = Query(True),
    url_filter: str = Query(""),
    context_filter: str = Query(""),
    group_filter: str = Query(""),
    file_name_filter: str = Query(""),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    links, total = await database.search_links(
        query=q,
        platform=platform or None,
        group_id=group_id,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
        show_dead=show_dead,
        dedupe=dedupe,
        url_filter=url_filter,
        context_filter=context_filter,
        group_filter=group_filter,
        file_name_filter=file_name_filter,
        date_from=date_from,
        date_to=date_to,
    )
    return {"links": links, "total": total, "limit": limit, "offset": offset}


@app.post("/api/links/backfill-magnets")
async def start_magnet_backfill():
    started = kick_magnet_backfill()
    if not started:
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    return {"ok": True}


@app.get("/api/links/backfill-magnets/status")
async def get_magnet_backfill_status():
    return dict(magnet_backfill_status)


@app.post("/api/links/{link_id}/retry-magnet-metadata")
async def retry_magnet_metadata(link_id: int):
    """Clear the sticky `magnet-enrich:*` probe error for a single magnet and
    kick aria2c+DHT one more time. Useful when a torrent went dark briefly
    and now has peers again."""
    row = await database._qrow(
        "SELECT url, platform, probe_error FROM links WHERE id = $1", link_id
    )
    if not row:
        raise HTTPException(404, "Link not found")
    if row["platform"] != "Magnet":
        raise HTTPException(400, "Not a magnet link")
    # Clear sticky error so the enrich pass picks it up again.
    await database._exec(
        "UPDATE links SET probe_error = NULL, probed_at = NULL WHERE id = $1",
        link_id,
    )
    # Fetch metadata synchronously so the UI can show success/failure now.
    import magnet_metadata
    try:
        meta = await magnet_metadata.fetch_magnet_metadata(row["url"], timeout=60)
    except Exception as e:
        await database.record_probe_result(
            link_id=link_id, available=None, files=[],
            error=f"magnet-enrich:{str(e)[:80]}",
        )
        return {"ok": False, "error": str(e)}
    if meta and meta.get("file_count"):
        files = magnet_metadata.magnet_to_link_files(meta)
        await database.record_probe_result(
            link_id=link_id, available=True, files=files, error=None,
        )
        return {"ok": True, "file_count": len(files)}
    await database.record_probe_result(
        link_id=link_id, available=None, files=[],
        error="magnet-enrich:no-metadata",
    )
    return {"ok": False, "error": "no peers / metadata unavailable"}


@app.post("/api/links/{link_id}/inspect-archives")
async def inspect_link_archives(link_id: int):
    """Partially download archive files inside a magnet torrent and return their contents.

    Uses aria2c with --bt-prioritize-piece=head,tail so only the header/footer
    pieces (where archive metadata lives) are fetched, not the full torrent.
    Results are stored in link_archive_contents and returned immediately.
    """
    row = await database._qrow(
        "SELECT url, platform, files_json FROM links WHERE id = $1", link_id
    )
    if not row:
        raise HTTPException(404, "Link not found")
    if row["platform"] != "Magnet":
        raise HTTPException(400, "Not a magnet link")

    magnet_uri = row["url"]
    if not magnet_uri.lower().startswith("magnet:"):
        raise HTTPException(400, "Invalid magnet URI")

    import archive_inspector
    try:
        results = await archive_inspector.inspect_magnet_archives(magnet_uri, timeout=150)
    except Exception as exc:
        logger.error("inspect-archives error for link %s: %s", link_id, exc)
        raise HTTPException(500, f"İnceleme başarısız: {exc}")

    if not results:
        return {"ok": False, "archives": {}, "reason": "no_archives_or_timeout"}

    # Persist to DB
    for archive_path, contents in results.items():
        await database.store_link_archive_contents(link_id, archive_path, contents)

    # Return with file counts
    out = {
        path: {"files": files, "count": len(files)}
        for path, files in results.items()
    }
    return {"ok": True, "archives": out}


@app.get("/api/links/{link_id}/archive-contents")
async def get_link_archive_contents_endpoint(link_id: int):
    """Return previously-inspected archive contents (without re-downloading)."""
    row = await database._qrow("SELECT id FROM links WHERE id = $1", link_id)
    if not row:
        raise HTTPException(404, "Link not found")
    contents = await database.get_link_archive_contents(link_id)
    return {"link_id": link_id, "archives": contents}


@app.post("/api/links/scan-archives")
async def start_archive_scan():
    """Toplu arşiv tarama: mevcut magnet linklerdeki ZIP/RAR/7z dosyalarını
    kısmen indirerek içeriklerini listeler ve DB'ye kaydeder."""
    started = kick_archive_scan()
    if not started:
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    return {"ok": True}


@app.get("/api/links/scan-archives/status")
async def get_archive_scan_status():
    return dict(archive_scan_status)


@app.post("/api/links/scan-archives/cancel")
async def cancel_archive_scan_endpoint():
    cancelled = cancel_archive_scan()
    return {"ok": True, "cancelled": cancelled}


@app.post("/api/links/retry-dead-magnets")
async def retry_dead_magnets_bulk():
    """Bulk: clear `magnet-enrich:*` errors on every dead magnet so the next
    magnet-backfill stage picks them up again. Returns the number of rows
    cleared. Does NOT trigger the backfill; user starts it via Hunter."""
    rows = await database._q(
        """UPDATE links
              SET probe_error = NULL, probed_at = NULL
            WHERE platform = 'Magnet'
              AND probe_error LIKE 'magnet-enrich:%'
            RETURNING id"""
    )
    return {"ok": True, "cleared": len(rows)}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    return await database.get_stats()


@app.get("/api/activity/heatmap")
async def activity_heatmap(group_id: Optional[int] = None):
    return await database.get_activity_heatmap(group_id)


@app.get("/api/export/files")
async def export_files():
    """Stream all files as a gzip-compressed TSV (channel \\t size \\t filename)."""
    import gzip, io
    from datetime import date as _date

    async def _generate():
        buf = io.BytesIO()
        gz  = gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6)
        # Column order kept stable; `source_type` appended so consumers that
        # only read the first three columns keep working. Values:
        #   telegram | torrent_inner | magnet_inner
        gz.write(b"channel\tfile_size\tfile_name\tsource_type\n")
        chunk_rows = 0
        async for row in database.export_files_cursor():
            ch   = f"@{row['username']}" if row['username'] else (row['group_name'] or '?')
            src  = row['source_type'] or 'telegram'
            line = f"{ch}\t{row['file_size'] or 0}\t{row['file_name'] or ''}\t{src}\n"
            gz.write(line.encode('utf-8', errors='replace'))
            chunk_rows += 1
            if chunk_rows >= 5000:
                gz.flush()
                chunk_rows = 0
                data = buf.getvalue()
                buf.seek(0); buf.truncate()
                if data:
                    yield data
        gz.close()
        tail = buf.getvalue()
        if tail:
            yield tail

    fname = f"telfiles_export_{_date.today().strftime('%Y%m%d')}.tsv.gz"
    return StreamingResponse(
        _generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Version + update check ───────────────────────────────────────────────────
# install.sh writes /app/version.json with the commit SHA at install time.
# The frontend hits /api/version on boot and shows a banner if origin/main
# has moved past the local commit. GitHub responses are cached for an hour
# so we don't burn the 60-req/h anon rate limit when many tabs are open.

_VERSION_CACHE: dict = {"checked_at": 0.0, "latest": None}
_VERSION_TTL_S = 3600.0
_GITHUB_REPO   = "enseitankado/telfiles"
_REPO_URL      = f"https://github.com/{_GITHUB_REPO}"


def _local_version() -> dict:
    try:
        with open(os.path.join(os.path.dirname(__file__), "version.json")) as f:
            d = _json.load(f) or {}
            return {
                "commit":      str(d.get("commit") or "")[:40],
                "commit_date": d.get("commit_date") or None,
            }
    except Exception:
        return {"commit": "", "commit_date": None}


async def _fetch_latest_commit() -> Optional[dict]:
    now = _time.time()
    if _VERSION_CACHE["latest"] and now - _VERSION_CACHE["checked_at"] < _VERSION_TTL_S:
        return _VERSION_CACHE["latest"]
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/commits/main",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "telfiles-update-check"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return _VERSION_CACHE["latest"]
                d = await r.json()
                msg = ((d.get("commit") or {}).get("message") or "").splitlines()
                out = {
                    "commit":      str(d.get("sha") or "")[:40],
                    "commit_date": ((d.get("commit") or {}).get("committer") or {}).get("date"),
                    "message":     msg[0] if msg else "",
                    "html_url":    d.get("html_url"),
                }
                _VERSION_CACHE["latest"]     = out
                _VERSION_CACHE["checked_at"] = now
                return out
    except Exception:
        return _VERSION_CACHE["latest"]


@app.get("/api/version")
async def get_version():
    local  = _local_version()
    latest = await _fetch_latest_commit()
    update_available = False
    if local.get("commit") and latest and latest.get("commit"):
        # SHAs may be full or short; one being a prefix of the other = equal.
        l, r = local["commit"], latest["commit"]
        update_available = not (l.startswith(r) or r.startswith(l))
    return {
        "local": local,
        "latest": latest,
        "update_available": update_available,
        "repo_url": _REPO_URL,
        "install_cmd": (
            "curl -fsSL https://raw.githubusercontent.com/"
            f"{_GITHUB_REPO}/main/install.sh | bash"
        ),
    }


@app.get("/api/status")
async def get_status():
    now = _time.time()
    cached = _status_cache.get("data")
    if cached and now - _status_cache.get("ts", 0) < 2.0:
        # Always return fresh sync + logs on top of cached DB data
        cached["sync"] = {**sync_status, "next_sync_at": _next_sync_at}
        cached["logs"] = list(_log_buffer)
        cached["system"]["uptime"] = now - _app_start
        return cached

    status_stats = await database.get_status_stats()
    base_stats   = await database.get_stats()

    result = {
        "files": {
            "total":          base_stats["total_files"],
            "total_size":     base_stats["total_size"],
            "downloaded":     base_stats["downloaded"],
            "downloaded_size": base_stats["downloaded_size"],
            "by_type":        status_stats["by_type"],
            "recent_24h":     status_stats["recent_24h"],
            "recent_7d":      status_stats["recent_7d"],
            "recent_30d":     status_stats["recent_30d"],
        },
        "links": {
            "total":       base_stats["total_links"],
            "by_platform": status_stats["by_platform"],
        },
        "groups":  status_stats["groups"],
        "db": {
            "tables":      status_stats["pg_tables"],
            "size_pretty": status_stats["pg_db_size_pretty"],
            "size_bytes":  status_stats["pg_db_size"],
        },
        "sync":   {**sync_status, "next_sync_at": _next_sync_at},
        "system": _read_sys_info(),
        "logs":   list(_log_buffer),
    }
    _status_cache["data"] = result
    _status_cache["ts"]   = now
    return result


# ── Watch terms & notifications ───────────────────────────────────────────────

class WatchRequest(BaseModel):
    keywords: str
    min_size_bytes: int = 0


@app.get("/api/watches")
async def list_watches():
    return await database.list_watches()


@app.post("/api/watches")
async def create_watch(req: WatchRequest):
    kw = (req.keywords or "").strip()
    if not kw:
        raise HTTPException(400, "keywords required")
    min_sz = max(0, int(req.min_size_bytes or 0))
    wid = await database.create_watch(kw, min_size_bytes=min_sz)
    return {"id": wid}


@app.delete("/api/watches/{watch_id}")
async def delete_watch(watch_id: int):
    await database.delete_watch(watch_id)
    return {"ok": True}


@app.get("/api/notifications")
async def list_notifications(active_only: bool = True):
    if active_only:
        return await database.list_active_notifications()
    return await database.list_all_notifications()


@app.post("/api/notifications/{notification_id}/dismiss")
async def dismiss_notification(notification_id: int):
    await database.dismiss_notification(notification_id)
    return {"ok": True}


# ── Watch notifications: push toggle (Telegram Saved Messages) ───────────────

class NotifySettingsRequest(BaseModel):
    tg_push_enabled: Optional[bool] = None


@app.get("/api/notify/settings")
async def api_get_notify_settings():
    return await database.get_notify_settings()


@app.put("/api/notify/settings")
async def api_set_notify_settings(req: NotifySettingsRequest):
    await database.set_notify_settings(tg_push_enabled=req.tg_push_enabled)
    return await database.get_notify_settings()


# ── Hunter (channel discovery) ────────────────────────────────────────────────

import hunter

class HunterSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    stage1_enabled: Optional[bool] = None
    stage2_enabled: Optional[bool] = None
    web_request_delay_ms: Optional[int] = None
    web_concurrency: Optional[int] = None
    tg_concurrency: Optional[int] = None
    tg_request_delay_ms: Optional[int] = None
    tg_daily_lookup_cap: Optional[int] = None
    tg_messages_to_sample: Optional[int] = None
    tg_account_id: Optional[int] = None
    tg_temp_join_enabled: Optional[bool] = None
    schedule_kind: Optional[str] = None
    schedule_interval_seconds: Optional[int] = None
    keywords: Optional[str] = None
    min_subscribers: Optional[int] = None
    languages: Optional[str] = None
    sources: Optional[str] = None
    ui_language: Optional[str] = None
    similar_expand_enabled: Optional[bool] = None
    similar_expand_max_per_seed: Optional[int] = None
    similar_expand_max_seeds: Optional[int] = None


@app.get("/api/hunter/settings")
async def hunter_get_settings():
    s = await database.get_hunter_settings()
    used_today = await database.hunter_lookups_today()
    return {**s, "lookups_used_today": used_today}


@app.put("/api/hunter/settings")
async def hunter_update_settings(req: HunterSettingsRequest):
    patch = {k: v for k, v in req.dict().items() if v is not None}
    await database.update_hunter_settings(patch)
    return {"ok": True}


@app.get("/api/hunter/status")
async def hunter_status_endpoint():
    return hunter.status


@app.get("/api/hunter/quota")
async def hunter_quota():
    """Telegram API quota snapshot for the quota lightbox.

    Returns live usage of our self-imposed daily lookup cap, the active
    FloodWait window (if any) persisted by Stage 3's cacheOnlyMode trigger,
    and a static catalog of Telegram methods we currently use (with their
    soft-cost class) plus methods we might wire up in the future. The
    catalog labels go through i18n on the frontend; numeric/state fields
    are computed here so the modal can render an exact countdown."""
    s = await database.get_hunter_settings()
    used_today = await database.hunter_lookups_today()
    cap = int(s.get("tg_daily_lookup_cap") or 0)
    remaining = max(0, cap - used_today) if cap else None

    # Seconds until the daily cap counter rolls over. Our hunter_lookups_today
    # bucket uses UTC date boundaries.
    now_utc = datetime.now(timezone.utc)
    midnight_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight_today + timedelta(days=1)
    day_reset_seconds = int((next_midnight - now_utc).total_seconds())

    # Active FloodWait (persisted on cacheOnlyMode trigger).
    fw_until = s.get("last_floodwait_until")
    fw_scope = s.get("last_floodwait_scope")
    fw_total = s.get("last_floodwait_seconds")
    fw_active = False
    fw_remaining = 0
    fw_until_iso = None
    if fw_until:
        try:
            if isinstance(fw_until, str):
                fw_until_dt = datetime.fromisoformat(fw_until.replace("Z", "+00:00"))
            else:
                fw_until_dt = fw_until
            if fw_until_dt.tzinfo is None:
                fw_until_dt = fw_until_dt.replace(tzinfo=timezone.utc)
            fw_remaining = int((fw_until_dt - now_utc).total_seconds())
            fw_active = fw_remaining > 0
            fw_until_iso = fw_until_dt.isoformat()
        except Exception:
            pass

    # Catalogue of Telegram methods. Each entry: id (stable), method (Telegram
    # API path), cost (light/moderate/strict), used (bool — whether this
    # codebase currently calls it). i18n keys on the frontend supply the
    # localized description per id.
    methods = [
        {"id": "resolveUsername",       "method": "contacts.resolveUsername",        "cost": "strict",   "used": True},
        {"id": "getFullChannel",        "method": "channels.getFullChannel",         "cost": "moderate", "used": True},
        {"id": "getHistory",            "method": "messages.getHistory",             "cost": "light",    "used": True},
        {"id": "searchInPeer",          "method": "messages.search (in-peer)",       "cost": "light",    "used": True},
        {"id": "getChannelRecommendations","method": "channels.getChannelRecommendations", "cost": "moderate", "used": True},
        {"id": "joinChannel",           "method": "channels.joinChannel",            "cost": "strict",   "used": True},
        {"id": "leaveChannel",          "method": "channels.leaveChannel",           "cost": "light",    "used": True},
        {"id": "getMessages",           "method": "channels.getMessages (by id)",    "cost": "light",    "used": True},
        {"id": "downloadDocument",      "method": "upload.getFile",                  "cost": "bandwidth","used": True},
        {"id": "getDialogs",            "method": "messages.getDialogs",             "cost": "moderate", "used": True},
        # Future / not yet wired
        {"id": "searchPosts",           "method": "channels.searchPosts",            "cost": "strict",   "used": False, "future": True, "premium": True},
        {"id": "contactsSearch",        "method": "contacts.search",                 "cost": "moderate", "used": False, "future": True},
        {"id": "searchGlobal",          "method": "messages.searchGlobal",           "cost": "moderate", "used": False, "future": True},
        {"id": "getAllStories",         "method": "stories.getAllStories",           "cost": "light",    "used": False, "future": True},
        {"id": "getParticipants",       "method": "channels.getParticipants",        "cost": "moderate", "used": False, "future": True, "note": "admin-only on most channels"},
    ]

    return {
        "lookups_used_today": used_today,
        "tg_daily_lookup_cap": cap,
        "lookups_remaining": remaining,
        "day_reset_seconds": day_reset_seconds,
        "floodwait": {
            "active": fw_active,
            "scope": fw_scope,
            "until_iso": fw_until_iso,
            "remaining_seconds": max(0, fw_remaining),
            "total_seconds": fw_total,
        },
        "methods": methods,
        "tg_account_id": int(s.get("tg_account_id") or 1),
        "now_iso": now_utc.isoformat(),
    }


@app.post("/api/hunter/run")
async def hunter_run():
    started = hunter.kick_run()
    return {"ok": True, "started": started}


@app.post("/api/hunter/enrich")
async def hunter_enrich_only():
    """Run Stage 3 (Telegram enrichment) standalone on `discovered` rows —
    triggered by the "Zenginleştir" toolbar button. Returns 409 if another
    hunter run is already in flight."""
    started = hunter.kick_enrich_only()
    if not started:
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    return {"ok": True}


@app.post("/api/hunter/cancel")
async def hunter_cancel_run():
    ok = hunter.request_cancel()
    return {"ok": ok}


@app.post("/api/hunter/skip_stage")
async def hunter_skip_stage():
    ok = hunter.request_skip_stage()
    return {"ok": ok}


@app.get("/api/hunter/candidates")
async def hunter_list_candidates(
    status: Optional[str] = Query(None),
    sort_by: str = Query("score"),
    sort_dir: str = Query("desc"),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
):
    rows, total = await database.list_hunter_candidates(
        status=status, sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset
    )
    # decode JSONB
    for r in rows:
        ftb = r.get("file_type_breakdown")
        if isinstance(ftb, str):
            try:
                import json as _j
                r["file_type_breakdown"] = _j.loads(ftb)
            except Exception:
                r["file_type_breakdown"] = {}
    return {"candidates": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/hunter/candidates/by_username/{username}")
async def hunter_get_by_username(username: str):
    c = await database.get_hunter_candidate_by_username(username)
    if not c:
        raise HTTPException(404, "Not a hunter candidate")
    ftb = c.get("file_type_breakdown")
    if isinstance(ftb, str):
        try:
            import json as _j
            c["file_type_breakdown"] = _j.loads(ftb)
        except Exception:
            c["file_type_breakdown"] = {}
    return c


@app.get("/api/hunter/candidates/{cid}")
async def hunter_get_candidate(cid: int):
    c = await database.get_hunter_candidate(cid)
    if not c:
        raise HTTPException(404, "Candidate not found")
    ftb = c.get("file_type_breakdown")
    if isinstance(ftb, str):
        try:
            import json as _j
            c["file_type_breakdown"] = _j.loads(ftb)
        except Exception:
            c["file_type_breakdown"] = {}
    return c


@app.post("/api/hunter/candidates/{cid}/join")
async def hunter_join(cid: int):
    return await hunter.join_candidate(cid)


@app.post("/api/hunter/candidates/{cid}/reject")
async def hunter_reject(cid: int):
    return await hunter.reject_candidate(cid)


@app.post("/api/hunter/candidates/{cid}/blacklist")
async def hunter_blacklist(cid: int, reason: Optional[str] = Query(None)):
    return await hunter.blacklist_candidate(cid, reason)


@app.post("/api/hunter/candidates/{cid}/restore")
async def hunter_restore(cid: int):
    return await hunter.restore_candidate(cid)


@app.get("/api/hunter/blacklist")
async def hunter_get_blacklist():
    return await database.list_blacklist()


@app.delete("/api/hunter/blacklist/{username}")
async def hunter_remove_blacklist(username: str):
    await database.remove_from_blacklist(username)
    return {"ok": True}


# ── Telemetry ────────────────────────────────────────────────────────────────
class TelemetryToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/telemetry/settings")
async def get_telemetry_settings_endpoint():
    s = await database.get_telemetry_settings()
    return {"enabled": bool(s.get("enabled"))}


@app.put("/api/telemetry/settings")
async def update_telemetry_settings_endpoint(req: TelemetryToggleRequest):
    await database.update_telemetry_settings({"enabled": req.enabled})
    return {"ok": True}


@app.get("/api/hunter/runs")
async def hunter_runs():
    return await database.list_hunter_runs(limit=30)


@app.get("/api/hunter/join_queue")
async def hunter_join_queue():
    return await database.list_join_queue()


@app.delete("/api/hunter/candidates")
async def hunter_clear_candidates():
    """Clear pending candidate rows (status discovered/enriched/reviewed/failed).
    Joined, rejected, blacklisted rows are PRESERVED so those decisions persist
    and rejected/blacklisted channels will not re-appear in future scans."""
    await database._exec(
        "DELETE FROM hunter_candidates "
        "WHERE status IN ('discovered','enriched','reviewed','failed')"
    )
    return {"ok": True}


@app.post("/api/hunter/backfill_peer_cache")
async def hunter_backfill_peer_cache(account_id: int = Query(1)):
    n = await hunter.backfill_peer_cache(account_id)
    return {"ok": True, "backfilled": n}


@app.post("/api/hunter/magnet_hunt/run")
async def hunter_magnet_hunt_run():
    started = hunter.kick_magnet_hunt()
    if not started:
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    return {"ok": True}


@app.post("/api/hunter/magnet_hunt/cancel")
async def hunter_magnet_hunt_cancel():
    cancelled = hunter.cancel_magnet_hunt()
    return {"ok": True, "cancelled": cancelled}


@app.get("/api/hunter/magnet_hunt/status")
async def hunter_magnet_hunt_status():
    return dict(hunter.magnet_hunt_status)


@app.post("/api/hunter/candidates/{cid}/deep_scan")
async def hunter_kick_deep_scan(cid: int):
    started = hunter.kick_deep_scan(cid)
    return {"ok": True, "started": started}


@app.post("/api/hunter/candidates/{cid}/deep_scan/cancel")
async def hunter_cancel_deep_scan(cid: int):
    cancelled = hunter.cancel_deep_scan(cid)
    return {"ok": True, "cancelled": cancelled}


@app.get("/api/hunter/candidates/{cid}/deep_scan_status")
async def hunter_deep_scan_status(cid: int):
    s = hunter.deep_scan_status.get(cid)
    if s is None:
        cand = await database.get_hunter_candidate(cid)
        if not cand:
            raise HTTPException(404, "Candidate not found")
        return {
            "state": cand.get("deep_scan_status"),
            "processed": cand.get("deep_scan_progress") or 0,
            "total": cand.get("deep_scan_total") or 0,
            "error": cand.get("deep_scan_error"),
            "at": cand.get("deep_scan_at"),
        }
    return s


@app.get("/api/hunter/candidates/{cid}/files")
async def hunter_candidate_files(
    cid: int,
    q: str = Query(""),
    ext: str = Query(""),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    limit: int = Query(200, le=2000),
    offset: int = Query(0),
):
    files, total = await database.list_candidate_files(
        cid, q=q, ext=ext, sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset,
    )
    summary = await database.candidate_file_summary(cid)
    return {"files": files, "total": total, "summary": summary,
            "limit": limit, "offset": offset}


# ── Per-file download from the candidate detail lightbox ─────────────────────
# Flow:
#  1. UI POSTs /download (no confirm). If the channel is public, we download
#     without joining and return state="done"/"downloading".
#  2. If the channel requires membership we set state="needs_temp_join". The
#     UI shows a confirm modal and re-POSTs with ?confirm_temp_join=1.
#  3. Polling /status drives progress UI. /blob streams the saved file once
#     state="done".

@app.post("/api/hunter/candidates/{cid}/files/{msg_id}/download")
async def hunter_file_download(
    cid: int, msg_id: int,
    confirm_temp_join: int = Query(0),
):
    import hunter as _hunter
    state = await _hunter.download_candidate_file(
        cid, msg_id, allow_temp_join=bool(confirm_temp_join),
    )
    return state


@app.get("/api/hunter/candidates/{cid}/files/{msg_id}/status")
async def hunter_file_download_status(cid: int, msg_id: int):
    import hunter as _hunter
    key = (int(cid), int(msg_id))
    state = _hunter.file_dl_status.get(key)
    if state:
        return state
    # Nothing in-memory — fall back to whatever is persisted on the row so a
    # reopened lightbox can still distinguish "already downloaded" from "never".
    cfile = await database.get_candidate_file(cid, msg_id)
    if cfile and cfile.get("local_path") and os.path.exists(cfile["local_path"]):
        return {"state": "done", "progress": 1.0,
                "local_path": cfile["local_path"]}
    return {"state": "idle"}


@app.post("/api/hunter/candidates/{cid}/files/{msg_id}/download/cancel")
async def hunter_file_download_cancel(cid: int, msg_id: int):
    import hunter as _hunter
    ok = await _hunter.cancel_candidate_file_download(cid, msg_id)
    return {"ok": bool(ok)}


@app.get("/api/hunter/candidates/{cid}/files/{msg_id}/blob")
async def hunter_file_blob(cid: int, msg_id: int):
    cfile = await database.get_candidate_file(cid, msg_id)
    if not cfile or not cfile.get("local_path"):
        raise HTTPException(404, "Henüz indirilmedi")
    p = cfile["local_path"]
    if not os.path.exists(p):
        await database.clear_candidate_file_local_path(cid, msg_id)
        raise HTTPException(404, "Dosya diskten silinmiş")
    return FileResponse(p, filename=cfile.get("file_name") or os.path.basename(p))


@app.get("/api/hunter/candidates/{cid}/files/{msg_id}/preview")
async def hunter_file_preview(cid: int, msg_id: int):
    import hunter as _hunter
    try:
        path, mime, fname = await _hunter.preview_candidate_file(cid, msg_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        msg = str(e)
        if msg.startswith("too_large:"):
            raise HTTPException(413, msg)
        raise HTTPException(404, msg)
    except Exception as e:
        logger.exception("hunter preview failed for cid=%s msg=%s: %s", cid, msg_id, e)
        raise HTTPException(500, f"preview_failed: {e}")
    headers = {"Content-Disposition": f"inline; filename=\"{fname}\""}
    return FileResponse(path, media_type=mime, headers=headers)


@app.delete("/api/hunter/candidates/{cid}/files/{msg_id}/blob")
async def hunter_file_blob_delete(cid: int, msg_id: int):
    cfile = await database.get_candidate_file(cid, msg_id)
    if not cfile:
        raise HTTPException(404, "Dosya bulunamadı")
    p = cfile.get("local_path")
    if p and os.path.exists(p):
        try: os.remove(p)
        except OSError: pass
    await database.clear_candidate_file_local_path(cid, msg_id)
    import hunter as _hunter
    _hunter.file_dl_status.pop((int(cid), int(msg_id)), None)
    return {"ok": True}


# ── Torrent endpoints ─────────────────────────────────────────────────────────

@app.get("/api/torrents/search")
async def torrent_content_search(
    q: str = Query("", min_length=1),
    limit: int = Query(100, ge=1, le=500),
):
    """Search inside torrent file contents via the trigram-indexed
    torrent_files table. Returns one dict per matching .torrent file."""
    if not q or not q.strip():
        return []
    return await database.search_torrent_files(q.strip(), limit=limit)


@app.post("/api/torrents/parse-all")
async def start_torrent_parse(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    concurrency = max(1, min(20, int(body.get("concurrency", 5))))
    started = _torrent_worker.start(concurrency)
    return {"started": started, "status": _torrent_worker.get_status()}


@app.get("/api/torrents/status")
async def get_torrent_status():
    counts = await database.count_torrents()
    return {"worker": _torrent_worker.get_status(), "counts": counts}


@app.post("/api/torrents/cancel")
async def cancel_torrent_parse():
    _torrent_worker.cancel()
    return {"ok": True}


@app.get("/api/files/{file_id}/torrent-tree")
async def get_file_torrent_tree(file_id: int):
    """Return cached tree or download+parse on demand (synchronous)."""
    try:
        result = await _torrent_parse.parse_single(file_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))
    # Return from DB (includes parsed_at timestamp)
    row = await database.get_torrent_tree(file_id)
    return row or result


# ── Static (must be last) ─────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="static", html=True), name="static")
