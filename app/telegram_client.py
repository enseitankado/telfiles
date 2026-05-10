"""Multi-account Telegram client manager.

Each account has its own Telethon TelegramClient instance and session file.
Sessions are stored at /app/data/accounts/{account_id}/telfiles.session
The pre-multi-account legacy session at /app/data/telfiles.session is kept as
the seed for Account 1 if accounts table was just bootstrapped.
"""
import json
import os
from typing import Dict, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import database

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
LEGACY_SESSION_PATH = os.path.join(DATA_DIR, "telfiles")
LEGACY_CREDS_PATH = os.path.join(DATA_DIR, "credentials.json")


# Per-account state (in-memory)
_clients: Dict[int, TelegramClient] = {}
_phone_code_hashes: Dict[int, str] = {}
_pending_phones: Dict[int, str] = {}


# Legacy globals (read by old code paths) — reflect default account 1.
API_ID: int = 0
API_HASH: str = ""


def _account_dir(account_id: int) -> str:
    p = os.path.join(ACCOUNTS_DIR, str(account_id))
    os.makedirs(p, exist_ok=True)
    return p


def _account_session_path(account_id: int) -> str:
    return os.path.join(_account_dir(account_id), "telfiles")


def _maybe_migrate_legacy_session(account_id: int):
    if account_id != 1:
        return
    target = _account_session_path(account_id) + ".session"
    if os.path.exists(target):
        return
    legacy = LEGACY_SESSION_PATH + ".session"
    if os.path.exists(legacy):
        try:
            import shutil
            shutil.copy(legacy, target)
        except Exception:
            pass


async def _get_account_creds(account_id: int) -> Tuple[Optional[int], Optional[str]]:
    a = await database.get_account(account_id)
    if a:
        return a["api_id"], a["api_hash"]
    return None, None


async def get_client(account_id: int = 1) -> TelegramClient:
    global API_ID, API_HASH
    c = _clients.get(account_id)
    if c is not None:
        return c
    api_id, api_hash = await _get_account_creds(account_id)
    if not api_id or not api_hash:
        raise RuntimeError(f"No credentials configured for account {account_id}")
    _maybe_migrate_legacy_session(account_id)
    sp = _account_session_path(account_id)
    client = TelegramClient(sp, api_id, api_hash)
    client.flood_sleep_threshold = 60
    _clients[account_id] = client
    if account_id == 1:
        API_ID, API_HASH = int(api_id), api_hash
    return client


def all_account_ids() -> list:
    return list(_clients.keys())


async def disconnect_account(account_id: int):
    c = _clients.pop(account_id, None)
    if c is not None:
        try:
            if c.is_connected():
                await c.disconnect()
        except Exception:
            pass
    _phone_code_hashes.pop(account_id, None)
    _pending_phones.pop(account_id, None)


async def disconnect_all():
    for aid in list(_clients.keys()):
        await disconnect_account(aid)


def _remove_session_files(account_id: int):
    sp = _account_session_path(account_id)
    for ext in (".session", ".session-journal"):
        p = sp + ext
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


async def set_credentials(account_id: int, api_id: int, api_hash: str, *, drop_session: bool = True):
    await disconnect_account(account_id)
    await database.update_account(account_id, api_id=int(api_id), api_hash=api_hash)
    if drop_session:
        _remove_session_files(account_id)


async def logout(account_id: int = 1):
    c = _clients.get(account_id)
    if c is not None:
        try:
            if c.is_connected():
                try:
                    await c.log_out()
                except Exception:
                    await c.disconnect()
        except Exception:
            pass
    await disconnect_account(account_id)
    _remove_session_files(account_id)


async def is_authorized(account_id: int = 1) -> bool:
    try:
        client = await get_client(account_id)
    except Exception:
        return False
    if not client.is_connected():
        try:
            await client.connect()
        except Exception:
            return False
    return await client.is_user_authorized()


async def send_code(account_id: int, phone: str):
    client = await get_client(account_id)
    if not client.is_connected():
        await client.connect()
    result = await client.send_code_request(phone)
    _phone_code_hashes[account_id] = result.phone_code_hash
    _pending_phones[account_id] = phone


async def sign_in_with_code(account_id: int, phone: str, code: str) -> dict:
    client = await get_client(account_id)
    pch = _phone_code_hashes.get(account_id)
    try:
        await client.sign_in(phone, code, phone_code_hash=pch)
        me = await client.get_me()
        await database.update_account(
            account_id, phone=phone,
            display_name=getattr(me, "first_name", None) or getattr(me, "username", None) or phone,
        )
        return {"ok": True, "name": me.first_name}
    except SessionPasswordNeededError:
        return {"ok": True, "needs_2fa": True}


async def sign_in_with_password(account_id: int, password: str) -> dict:
    client = await get_client(account_id)
    await client.sign_in(password=password)
    me = await client.get_me()
    phone = _pending_phones.get(account_id)
    await database.update_account(
        account_id,
        phone=phone,
        display_name=getattr(me, "first_name", None) or getattr(me, "username", None),
    )
    return {"ok": True, "name": me.first_name}
