import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, List, Tuple

from telethon import events
from telethon.tl.types import (
    Channel,
    DocumentAttributeFilename,
    InputMessagesFilterDocument,
    InputMessagesFilterUrl,
    MessageEntityUrl,
    MessageEntityTextUrl,
)

import database
from telegram_client import get_client

logger = logging.getLogger("sync")

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/app/downloads")

# Per-account sync status: {account_id: status_dict}
sync_status_by_account: dict = {}

# Aggregate / legacy single-account view (rebuilt on every read via _aggregate_status)
sync_status: dict = {
    "running": False,
    "current_group": None,
    "processed_groups": 0,
    "total_groups": 0,
    "new_files": 0,
    "new_links": 0,
    "last_sync_at": None,
    "error": None,
}


def _new_status_dict() -> dict:
    return {
        "running": False,
        "current_group": None,
        "processed_groups": 0,
        "total_groups": 0,
        "new_files": 0,
        "new_links": 0,
        "last_sync_at": None,
        "error": None,
    }


def _get_account_status(account_id: int) -> dict:
    s = sync_status_by_account.get(account_id)
    if s is None:
        s = _new_status_dict()
        sync_status_by_account[account_id] = s
    return s


def _refresh_aggregate_status():
    """Update the legacy `sync_status` to a sensible aggregate of all accounts.
    `running` = any account running.
    `processed/total/new_*` = sum across accounts (current session).
    `current_group` = first running account's current group.
    `last_sync_at` = max across accounts.
    `error` = first non-empty error.
    """
    sync_status["running"] = any(s["running"] for s in sync_status_by_account.values())
    sync_status["processed_groups"] = sum(s["processed_groups"] for s in sync_status_by_account.values())
    sync_status["total_groups"]     = sum(s["total_groups"] for s in sync_status_by_account.values())
    sync_status["new_files"]        = sum(s["new_files"] for s in sync_status_by_account.values())
    sync_status["new_links"]        = sum(s["new_links"] for s in sync_status_by_account.values())
    cur = None
    for s in sync_status_by_account.values():
        if s["running"] and s["current_group"]:
            cur = s["current_group"]; break
    sync_status["current_group"] = cur
    last_at = None
    for s in sync_status_by_account.values():
        v = s.get("last_sync_at")
        if v and (last_at is None or v > last_at):
            last_at = v
    sync_status["last_sync_at"] = last_at
    err = None
    for s in sync_status_by_account.values():
        if s.get("error"):
            err = s["error"]; break
    sync_status["error"] = err

# (platform_name, domain_substring) — checked via `domain_substring in url.lower()`
_PLATFORMS: List[Tuple[str, str]] = [
    ("Google Drive", "drive.google.com"),
    ("Google Drive", "docs.google.com"),
    ("Mega", "mega.nz"),
    ("Mega", "mega.co.nz"),
    ("MediaFire", "mediafire.com"),
    ("Dropbox", "dropbox.com"),
    ("WeTransfer", "wetransfer.com"),
    ("OneDrive", "onedrive.live.com"),
    ("OneDrive", "1drv.ms"),
    ("Box", "box.com"),
    ("Yandex Disk", "disk.yandex."),
    ("Yandex Disk", "yadi.sk"),
    ("pCloud", "pcloud.com"),
    ("Zippyshare", "zippyshare.com"),
    ("Uploaded", "uploaded.net"),
    ("Rapidgator", "rapidgator.net"),
    ("Nitroflare", "nitroflare.com"),
    ("Gofile", "gofile.io"),
    ("Pixeldrain", "pixeldrain.com"),
    ("Sendspace", "sendspace.com"),
    ("4shared", "4shared.com"),
    ("Turbobit", "turbobit.net"),
    ("Katfile", "katfile.com"),
    ("Mixdrop", "mixdrop.co"),
    ("Cyberdrop", "cyberdrop.me"),
    ("Streamtape", "streamtape.com"),
    ("Doodstream", "doodstream.com"),
    ("1Fichier", "1fichier.com"),
    ("Hexupload", "hexupload.net"),
    ("Filecrypt", "filecrypt.cc"),
    ("Anonfiles", "anonfiles.com"),
    ("Bayfiles", "bayfiles.com"),
    ("Multiup", "multiup.org"),
    ("Filesfly", "filesfly.com"),
]


def _detect_platform(url: str) -> Optional[str]:
    """Return the platform name if url matches a known file-hosting domain, else None."""
    lower = url.lower()
    for platform, domain in _PLATFORMS:
        if domain in lower:
            return platform
    return None


def _slice_utf16(text: str, offset: int, length: int) -> str:
    """Telegram entity offset/length are UTF-16 code units, not Python code
    points. A surrogate-pair character (e.g. most emoji, regional-indicator
    flags) is 1 Python char but 2 UTF-16 units, so slicing the Python string
    directly drops 1 prefix char per surrogate pair before the URL and pulls
    in junk past the URL. Encode → byte-slice → decode keeps everything
    aligned with what Telegram thinks."""
    try:
        b = text.encode('utf-16-le', errors='replace')
        sliced = b[offset * 2: (offset + length) * 2]
        return sliced.decode('utf-16-le', errors='replace')
    except Exception:
        return ''


# Anything past the URL itself is invalid — strip whitespace, quotes, and
# common trailing punctuation that messages tack on (")", "]", "?!.,;…", etc.)
_URL_TRAILING_TRIM = '\n\r\t \xa0"\'<>()[]{}«»“”‘’,;:!?.…'


def _trim_url(s: str) -> str:
    s = (s or '').strip()
    while s and s[-1] in _URL_TRAILING_TRIM:
        s = s[:-1]
    # Sometimes the slice still includes a newline + extra text; cut at the
    # first whitespace which is never legal inside a URL.
    for ws in ('\n', '\r', '\t', ' '):
        i = s.find(ws)
        if i > 0:
            s = s[:i]
            break
    return s


def _extract_platform_urls(message) -> List[Tuple[str, str]]:
    """
    Iterate message.entities and return a list of (platform, url) tuples
    for every URL entity whose URL matches a known file-hosting platform.
    """
    results: List[Tuple[str, str]] = []
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""

    for entity in entities:
        url: Optional[str] = None
        if isinstance(entity, MessageEntityUrl):
            url = _slice_utf16(text, entity.offset, entity.length)
        elif isinstance(entity, MessageEntityTextUrl):
            # MessageEntityTextUrl carries the URL itself in entity.url, so
            # no slicing is needed.
            url = entity.url

        url = _trim_url(url)
        if not url:
            continue

        platform = _detect_platform(url)
        if platform:
            results.append((platform, url))

    return results


def _extract_file_info(message) -> Optional[dict]:
    doc = getattr(message, "document", None)
    if not doc:
        return None

    file_name: Optional[str] = None
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            file_name = attr.file_name
            break

    if not file_name:
        mime = getattr(doc, "mime_type", "") or ""
        ext = mime.split("/")[-1] if "/" in mime else "bin"
        file_name = f"file_{message.id}.{ext}"

    file_ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    return {
        "file_name": file_name,
        "file_ext": file_ext,
        "mime_type": getattr(doc, "mime_type", None),
        "file_size": getattr(doc, "size", 0) or 0,
    }


async def _sync_group(account_id: int, client, entity, group_id: int, group_name: str) -> int:
    last_id = await database.get_last_synced_message_id_for_account(account_id, group_id)
    new_files = 0
    max_id = last_id

    try:
        async for message in client.iter_messages(
            entity,
            filter=InputMessagesFilterDocument,
            min_id=last_id,
            reverse=True,
        ):
            info = _extract_file_info(message)
            if not info:
                continue

            date_str = (
                message.date.isoformat() if message.date else datetime.utcnow().isoformat()
            )
            context = (
                (getattr(message, "text", None) or getattr(message, "caption", None) or "")[:300]
            ) or None

            inserted = await database.insert_file(
                group_id=group_id,
                message_id=message.id,
                **info,
                date=date_str,
                context=context,
                discovered_by_account_id=account_id,
            )
            if inserted:
                new_files += 1

            if message.id > max_id:
                max_id = message.id

        if max_id > last_id:
            await database.update_last_synced_for_account(account_id, group_id, max_id)

    except Exception as e:
        logger.warning(f"[acc {account_id}] Error syncing '{group_name}': {e}")

    return new_files


async def _sync_group_links(account_id: int, client, entity, group_id: int, group_name: str) -> int:
    last_id = await database.get_last_synced_link_id_for_account(account_id, group_id)
    new_links = 0
    max_id = last_id

    try:
        async for message in client.iter_messages(
            entity,
            filter=InputMessagesFilterUrl,
            min_id=last_id,
            reverse=True,
        ):
            platform_urls = _extract_platform_urls(message)
            if not platform_urls:
                continue

            date_str = (
                message.date.isoformat() if message.date else datetime.utcnow().isoformat()
            )
            context = (
                (message.text or getattr(message, "caption", None) or "")[:300]
            )

            for platform, url in platform_urls:
                inserted = await database.insert_link(
                    group_id=group_id,
                    message_id=message.id,
                    platform=platform,
                    url=url,
                    context=context,
                    date=date_str,
                    discovered_by_account_id=account_id,
                )
                if inserted:
                    new_links += 1

            if message.id > max_id:
                max_id = message.id

        if max_id > last_id:
            await database.update_last_synced_links_for_account(account_id, group_id, max_id)

    except Exception as e:
        logger.warning(f"[acc {account_id}] Error syncing links for '{group_name}': {e}")

    return new_links


async def _refresh_member_counts(client, account_id: int, batch_size: int = 10):
    """Best-effort: refresh member_count for a small batch of stale-or-empty
    groups. Uses the int group_id (already in the Telethon session cache from
    iter_dialogs) so no ResolveUsername quota is consumed."""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest as _GetFull
        from telethon.errors import FloodWaitError as _Flood
        rows = await database.get_groups_needing_member_count(limit=batch_size)
        for r in rows:
            try:
                entity = await client.get_input_entity(int(r["id"]))
                if not entity:
                    continue
                full = await client(_GetFull(entity))
                cnt = getattr(full.full_chat, "participants_count", None)
                if cnt is not None:
                    await database.update_group_member_count(int(r["id"]), int(cnt))
                await asyncio.sleep(0.5)
            except _Flood:
                break    # respect flood, finish later
            except Exception:
                continue
    except Exception:
        pass


async def run_sync_account(account_id: int):
    """Sync a single account's groups."""
    s = _get_account_status(account_id)
    if s["running"]:
        logger.info(f"[acc {account_id}] sync already running, skipping.")
        return

    s.update({"running": True, "error": None, "new_files": 0, "new_links": 0,
              "processed_groups": 0, "total_groups": 0, "current_group": None})
    _refresh_aggregate_status()

    try:
        client = await get_client(account_id)
        if not client.is_connected():
            await client.connect()

        excluded = set(await database.get_excluded_group_ids_for_account(account_id))

        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                gid = dialog.id
                if gid in excluded:
                    continue
                name = dialog.name or f"Group {gid}"
                username = getattr(entity, "username", None)
                is_channel = isinstance(entity, Channel)
                await database.upsert_group(gid, name, username, is_channel)
                await database.upsert_account_group(account_id, gid)
                # Capture participants_count from the dialog response (no
                # ResolveUsername required — entity already in session cache).
                pc = getattr(entity, "participants_count", None)
                if pc is not None:
                    try:
                        await database.update_group_member_count(int(gid), int(pc))
                    except Exception:
                        pass
                dialogs.append((gid, entity, name))

        s["total_groups"] = len(dialogs)
        s["processed_groups"] = 0
        _refresh_aggregate_status()

        for gid, entity, name in dialogs:
            s["current_group"] = name
            _refresh_aggregate_status()
            count = await _sync_group(account_id, client, entity, gid, name)
            link_count = await _sync_group_links(account_id, client, entity, gid, name)
            s["new_files"] += count
            s["new_links"] += link_count
            s["processed_groups"] += 1
            _refresh_aggregate_status()
            if count > 0 or link_count > 0:
                logger.info(
                    f"[acc {account_id} {s['processed_groups']}/{s['total_groups']}]"
                    f" {name}: {count} new files, {link_count} new links"
                )

        s["last_sync_at"] = datetime.utcnow().isoformat()
        logger.info(
            f"[acc {account_id}] Sync complete. New files: {s['new_files']}, "
            f"new links: {s['new_links']}"
        )

        # Best-effort: refresh member_count for ~10 stale groups per account
        try:
            await _refresh_member_counts(client, account_id, batch_size=10)
        except Exception as e:
            logger.debug(f"member_count refresh skipped: {e}")

    except Exception as e:
        s["error"] = str(e)
        logger.error(f"[acc {account_id}] Sync failed: {e}", exc_info=True)

    finally:
        s["running"] = False
        s["current_group"] = None
        _refresh_aggregate_status()


async def run_sync():
    """Sync ALL active accounts in parallel."""
    accounts = await database.list_accounts()
    active = [a for a in accounts if a.get("is_active", True)]
    if not active:
        logger.info("No active accounts to sync.")
        return
    tasks = [asyncio.create_task(run_sync_account(a["id"])) for a in active]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Watch check after all accounts done
        try:
            new_matches = await database.check_watches()
            if new_matches > 0:
                logger.info(f"Watch terms matched {new_matches} new file(s)")
        except Exception as e:
            logger.warning(f"Watch check failed: {e}")
        _refresh_aggregate_status()


def setup_realtime_handler(client, account_id: int = 1):
    @client.on(events.NewMessage)
    async def _on_new_message(event):
        msg = event.message

        try:
            chat = await event.get_chat()
        except Exception:
            return

        gid = event.chat_id
        name = getattr(chat, "title", str(gid))
        username = getattr(chat, "username", None)
        is_channel = isinstance(chat, Channel)

        await database.upsert_group(gid, name, username, is_channel)
        await database.upsert_account_group(account_id, gid)

        # Honor per-account "Takip Etme" — skip realtime indexing for excluded groups
        excluded = await database.get_excluded_group_ids_for_account(account_id)
        if gid in set(excluded):
            return

        date_str = (
            msg.date.isoformat() if msg.date else datetime.utcnow().isoformat()
        )

        if msg.document:
            info = _extract_file_info(msg)
            if info:
                inserted = await database.insert_file(
                    group_id=gid,
                    message_id=msg.id,
                    **info,
                    date=date_str,
                    discovered_by_account_id=account_id,
                )
                if inserted:
                    logger.info(f"[realtime acc {account_id}] {name}: {info['file_name']}")

        platform_urls = _extract_platform_urls(msg)
        if platform_urls:
            context = (
                (msg.text or getattr(msg, "caption", None) or "")[:300]
            )
            any_link_inserted = False
            for platform, url in platform_urls:
                inserted = await database.insert_link(
                    group_id=gid,
                    message_id=msg.id,
                    platform=platform,
                    url=url,
                    context=context,
                    date=date_str,
                    discovered_by_account_id=account_id,
                )
                if inserted:
                    any_link_inserted = True
            if any_link_inserted:
                logger.info(
                    f"[realtime acc {account_id}] {name}: {len(platform_urls)} link(s) indexed"
                )


# Active downloads keyed by file id so they can be cancelled mid-flight
_download_tasks: dict = {}


def cancel_download(file_id: int) -> bool:
    """Request cancellation of an in-flight download. Returns True if a
    matching task was found and cancellation was requested."""
    task = _download_tasks.get(file_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def download_file(file_id: int) -> str:
    file = await database.get_file_by_id(file_id)
    if not file:
        raise ValueError(f"File {file_id} not found")

    if file["local_path"] and os.path.exists(file["local_path"]):
        return file["local_path"]

    # Register this task so the cancel endpoint can find it
    task = asyncio.current_task()
    if task is not None:
        _download_tasks[file_id] = task

    await database.set_file_downloading(file_id, True, 0.0)
    # Use the account that discovered the file (falls back to account 1)
    acc_id = file.get("discovered_by_account_id") or 1
    try:
        client = await get_client(acc_id)
    except Exception:
        client = await get_client(1)
    if not client.is_connected():
        await client.connect()
    dest = None

    try:
        safe_group = "".join(
            c for c in (file["group_name"] or str(file["group_id"]))
            if c.isalnum() or c in " _-"
        ).strip()[:60] or str(file["group_id"])

        dest_dir = os.path.join(DOWNLOADS_DIR, safe_group)
        os.makedirs(dest_dir, exist_ok=True)

        file_name = file["file_name"] or f"file_{file['message_id']}"
        dest = os.path.join(dest_dir, file_name)

        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            dest = f"{base}_{file['message_id']}{ext}"

        message = await client.get_messages(file["group_id"], ids=file["message_id"])
        if not message or not message.document:
            raise ValueError("Message or document not found on Telegram")

        async def _progress(current, total):
            if total:
                await database.set_file_downloading(file_id, True, current / total)

        await client.download_media(message, dest, progress_callback=_progress)
        await database.set_file_local_path(file_id, dest)
        logger.info(f"Downloaded: {dest}")
        return dest

    except asyncio.CancelledError:
        # User cancelled — drop any partial file from disk and reset state
        if dest and os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        await database.set_file_downloading(file_id, False, 0.0)
        logger.info(f"Download cancelled: file_id={file_id}")
        raise
    except Exception:
        await database.set_file_downloading(file_id, False, 0.0)
        raise
    finally:
        _download_tasks.pop(file_id, None)
