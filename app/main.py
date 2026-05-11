import asyncio
import collections
import logging
import os
from datetime import datetime, timedelta
import time as _time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

import ui_auth

import database
import telegram_client
from sync import (
    cancel_download,
    download_file,
    run_sync,
    setup_realtime_handler,
    sync_status,
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
            now = datetime.utcnow()
            nxt = s.get("next_run_at")
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


def _start_background_tasks():
    global _auto_sync_task, _next_sync_at, _hunter_loop_task, _link_probe_task, _join_queue_task
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
    # Pre-warm the Files grid cache so the first tab-click after boot is
    # instant. The dedupe rows query takes ~2s cold; running it once at
    # startup populates the in-process cache. Fire-and-forget; if it fails
    # the user just pays the usual cold cost.
    asyncio.create_task(_prewarm_files_cache())


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
    await database.update_account(account_id, name=req.name, is_active=req.is_active)
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
        raise HTTPException(401, "Not authorized — log in again")

    try:
        entity = await client.get_entity(group_id)
        # delete_dialog handles channels, supergroups and basic groups uniformly
        await client.delete_dialog(entity)
    except Exception as e:
        raise HTTPException(500, f"Telegram'dan ayrılınamadı: {e}")

    if purge:
        await database.delete_group_data(group_id)
    else:
        # Mark excluded for the leaving account; keep other accounts as-is
        await database.set_account_group_settings(account_id, group_id, excluded=1)
        # Legacy compat: also flip the global flag
        await database.set_group_settings(group_id, None, 1, None)

    logger.info(f"Left Telegram dialog: {g.get('name')} ({group_id}), purge={purge}")
    return {"ok": True, "group_id": group_id, "name": g.get("name"), "purged": purge}


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
    files, total = await database.search_files(
        query=q, ext=ext, ext_group=ext_group, group_id=group_id, group_ids=gids, file_ids=fids,
        date_from=date_from, date_to=date_to,
        size_min=size_min, size_max=size_max,
        sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset,
        dedupe=dedupe,
    )
    return {"files": files, "total": total, "limit": limit, "offset": offset}


@app.get("/api/files/{file_id}")
async def get_file(file_id: int):
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    return f


@app.post("/api/files/{file_id}/download")
async def trigger_download(file_id: int, background_tasks: BackgroundTasks):
    f = await database.get_file_by_id(file_id)
    if not f:
        raise HTTPException(404, "File not found")

    if f["local_path"] and os.path.exists(f["local_path"]):
        return {"status": "already_downloaded", "path": f["local_path"]}

    if f["downloading"]:
        return {"status": "downloading", "progress": f["download_progress"]}

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


@app.get("/api/downloads")
async def list_downloads():
    return await database.list_downloaded_files()


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
    min_files: Optional[int] = Query(None),
    max_files: Optional[int] = Query(None),
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
        min_files=min_files,
        max_files=max_files,
        date_from=date_from,
        date_to=date_to,
    )
    return {"links": links, "total": total, "limit": limit, "offset": offset}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    return await database.get_stats()


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


@app.get("/api/watches")
async def list_watches():
    return await database.list_watches()


@app.post("/api/watches")
async def create_watch(req: WatchRequest):
    kw = (req.keywords or "").strip()
    if not kw:
        raise HTTPException(400, "keywords required")
    wid = await database.create_watch(kw)
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


@app.post("/api/hunter/run")
async def hunter_run():
    started = hunter.kick_run()
    return {"ok": True, "started": started}


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
    sort: str = Query("score"),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
):
    rows, total = await database.list_hunter_candidates(status=status, sort=sort, limit=limit, offset=offset)
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


# ── Static (must be last) ─────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="static", html=True), name="static")
