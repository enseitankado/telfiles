import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from urllib.parse import parse_qs

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
import torrent_parse
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
    # NOTE: Zippyshare (closed Mar 2023), Uploaded/uploaded.net (closed 2024),
    # Anonfiles/Bayfiles (closed Aug 2023) used to be listed here. They are
    # removed from the classifier — startup also DELETEs any existing links
    # with these platform names from the DB (see database.delete_dead_platforms).
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
    ("Bunkr", "bunkr.is"),
    ("Bunkr", "bunkr.su"),
    ("Bunkr", "bunkr.la"),
    ("Bunkr", "bunkr.cr"),
    ("Bunkr", "bunkr.ph"),
    ("Bunkr", "bunkr.fi"),
    ("Bunkr", "bunkr.media"),
    ("Bunkr", "bunkrr.su"),
    ("Krakenfiles", "krakenfiles.com"),
    ("Catbox", "files.catbox.moe"),
    ("Litterbox", "litterbox.catbox.moe"),
    ("Litterbox", "litter.catbox.moe"),
    ("Streamtape", "streamtape.com"),
    ("Doodstream", "doodstream.com"),
    ("1Fichier", "1fichier.com"),
    ("Hexupload", "hexupload.net"),
    ("Filecrypt", "filecrypt.cc"),
    ("Multiup", "multiup.org"),
    ("Filesfly", "filesfly.com"),
    # ── 2026-07: düşük-puanlı kanalların mesaj taramasıyla tespit edilen host'lar ──
    ("RSLinks", "rslinks.net"),
    ("10Drives", "10drives.com"),
    ("DevUploads", "devuploads.com"),
    ("APMFile", "apmfile.com"),
    ("FTUApps", "farlad.com"),
    ("FreeCracks", "freecracksdownload.com"),
    ("Hide01", "hide01.ir"),
    ("Zoom-Platform", "zoom-platform.folktec.com"),
    ("Telegraph", "telegra.ph/file/"),
    ("GPLinks", "gplinks.co"),
    ("GPLinks", "gplinks.pro"),
    ("Magfi", "magfi.link"),
    ("Linktw", "linktw.in"),
]


def _detect_platform(url: str) -> Optional[str]:
    """Return the platform name if url matches a known file-hosting domain, else None."""
    if url.lower().startswith('magnet:'):
        return "Magnet"
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


_MAGNET_RE = re.compile(r'magnet:\?[^\s<>"\']+', re.I)
_HTTP_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)


import re as _re_sync
_SYNC_BTIH_HEX_RE = _re_sync.compile(r"^[A-Fa-f0-9]{40}$")
_SYNC_BTIH_B32_RE = _re_sync.compile(r"^[A-Z2-7]{32}$")


def _is_valid_btih(infohash: str) -> bool:
    """Real BitTorrent v1 info-hash: 40 hex or 32 base32 characters. Rejects
    SERP-scrape pollution like `%22%20database` that decodes to plain text."""
    if not infohash:
        return False
    from urllib.parse import unquote as _unq
    s = _unq(infohash).strip()
    return bool(_SYNC_BTIH_HEX_RE.match(s) or _SYNC_BTIH_B32_RE.match(s.upper()))


def _parse_magnet_info(uri: str) -> dict:
    if '?' not in uri:
        return {}
    qs_str = uri.split('?', 1)[1]
    qs = parse_qs(qs_str, keep_blank_values=False)
    xt = (qs.get('xt') or [''])[0]
    infohash = ''
    if 'urn:btih:' in xt.lower():
        raw = xt.lower().split('urn:btih:', 1)[1]
        infohash = raw.split('&')[0].strip()
    # Drop anything that's not a real info-hash — otherwise garbage magnets
    # from SERP scrapes (e.g. xt=urn:btih:%22%20database) get persisted with
    # a single placeholder file and pollute the Links grid.
    if not _is_valid_btih(infohash):
        return {}
    name = (qs.get('dn') or [''])[0]
    try:
        size = int((qs.get('xl') or ['0'])[0])
    except (ValueError, TypeError):
        size = 0
    return {'infohash': infohash, 'name': name, 'size': size}


def _magnet_link_data(uri: str) -> dict:
    """Return pre-parsed probe fields for a magnet URI (no HTTP needed)."""
    info = _parse_magnet_info(uri)
    infohash = info.get('infohash', '')
    if not infohash:
        return {}
    name = info.get('name') or f"Magnet {infohash[:8].upper()}…"
    size = info.get('size', 0)
    return {
        'files_json': [{'name': name, 'size': size}],
        'available': True,
        'file_count': 1,
        'file_size_total': size,
    }


def _extract_all_magnet_urls(message) -> List[str]:
    """Extract every magnet URI from a message (entities + raw text), deduped.
    Filtered through _parse_magnet_info so only URIs with a valid BitTorrent
    info-hash survive."""
    seen: set = set()
    results: List[str] = []
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""

    def _accept(url: str) -> bool:
        return bool(url and url.lower().startswith('magnet:')
                    and url not in seen
                    and _parse_magnet_info(url))

    for entity in entities:
        url: Optional[str] = None
        if isinstance(entity, MessageEntityUrl):
            url = _trim_url(_slice_utf16(text, entity.offset, entity.length))
        elif isinstance(entity, MessageEntityTextUrl):
            url = _trim_url(entity.url or '')
        if url and _accept(url):
            seen.add(url)
            results.append(url)
    for m in _MAGNET_RE.finditer(text):
        uri = _trim_url(m.group(0))
        if _accept(uri):
            seen.add(uri)
            results.append(uri)
    return results


def _extract_platform_urls(message) -> List[Tuple[str, str]]:
    """
    Return (platform, url) tuples for every URL matching a known file-hosting
    platform. Scans BOTH message.entities AND the raw text (regex) — some
    channels post links as plain text without an auto-link entity (code blocks,
    unusual TLDs, copy-paste), which the entity-only pass used to miss. Deduped.
    """
    results: List[Tuple[str, str]] = []
    seen: set = set()
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""

    def _add(raw_url: Optional[str]) -> None:
        url = _trim_url(raw_url)
        if not url or url in seen:
            return
        seen.add(url)
        platform = _detect_platform(url)
        if platform:
            results.append((platform, url))

    for entity in entities:
        if isinstance(entity, MessageEntityUrl):
            _add(_slice_utf16(text, entity.offset, entity.length))
        elif isinstance(entity, MessageEntityTextUrl):
            # MessageEntityTextUrl carries the URL itself in entity.url.
            _add(entity.url)

    # Plain-text URLs (no entity) — mirror the magnet extractor's text scan.
    for m in _HTTP_URL_RE.finditer(text):
        _add(m.group(0))

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


async def _sync_group(account_id: int, client, entity, group_id: int, group_name: str) -> Tuple[int, List[int]]:
    last_id = await database.get_last_synced_message_id_for_account(account_id, group_id)
    new_files = 0
    new_torrent_ids: List[int] = []
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

            file_id = await database.insert_file(
                group_id=group_id,
                message_id=message.id,
                **info,
                date=date_str,
                context=context,
                discovered_by_account_id=account_id,
            )
            if file_id is not None:
                new_files += 1
                if info.get("file_ext") == "torrent":
                    new_torrent_ids.append(file_id)

            if message.id > max_id:
                max_id = message.id

        if max_id > last_id:
            await database.update_last_synced_for_account(account_id, group_id, max_id)

    except Exception as e:
        logger.warning(f"[acc {account_id}] Error syncing '{group_name}': {e}")

    return new_files, new_torrent_ids


async def _auto_parse_torrents(file_ids: List[int]):
    """Background task: parse newly synced .torrent files one by one."""
    for fid in file_ids:
        try:
            await torrent_parse.parse_single(fid)
        except Exception as e:
            logger.debug(f"[auto-parse] torrent {fid}: {e}")
        await asyncio.sleep(0.5)


async def _sync_group_links(account_id: int, client, entity, group_id: int, group_name: str) -> int:
    last_id = await database.get_last_synced_link_id_for_account(account_id, group_id)
    new_links = 0
    max_id = last_id

    # Pass 1: URL-entity messages — covers platform URLs and magnet: URL entities
    try:
        async for message in client.iter_messages(
            entity,
            filter=InputMessagesFilterUrl,
            min_id=last_id,
            reverse=True,
        ):
            platform_urls = _extract_platform_urls(message)
            if not platform_urls:
                if message.id > max_id:
                    max_id = message.id
                continue

            date_str = (
                message.date.isoformat() if message.date else datetime.utcnow().isoformat()
            )
            context = (
                (message.text or getattr(message, "caption", None) or "")[:300]
            )

            for platform, url in platform_urls:
                link_data = _magnet_link_data(url) if platform == "Magnet" else {}
                inserted = await database.insert_link(
                    group_id=group_id,
                    message_id=message.id,
                    platform=platform,
                    url=url,
                    context=context,
                    date=date_str,
                    discovered_by_account_id=account_id,
                    **link_data,
                )
                if inserted:
                    new_links += 1

            if message.id > max_id:
                max_id = message.id

    except Exception as e:
        logger.warning(f"[acc {account_id}] Error syncing links for '{group_name}': {e}")

    # Pass 2: full-text search for "magnet:" — catches plain-text magnets without URL entities
    try:
        async for message in client.iter_messages(
            entity,
            search="magnet:",
            min_id=last_id,
            reverse=True,
        ):
            magnet_urls = _extract_all_magnet_urls(message)
            if not magnet_urls:
                if message.id > max_id:
                    max_id = message.id
                continue
            date_str = (
                message.date.isoformat() if message.date else datetime.utcnow().isoformat()
            )
            context = (
                (message.text or getattr(message, "caption", None) or "")[:300]
            )
            for uri in magnet_urls:
                link_data = _magnet_link_data(uri)
                inserted = await database.insert_link(
                    group_id=group_id,
                    message_id=message.id,
                    platform="Magnet",
                    url=uri,
                    context=context,
                    date=date_str,
                    discovered_by_account_id=account_id,
                    **link_data,
                )
                if inserted:
                    new_links += 1
            if message.id > max_id:
                max_id = message.id
    except Exception as e:
        logger.warning(f"[acc {account_id}] Error in magnet search for '{group_name}': {e}")

    if max_id > last_id:
        await database.update_last_synced_links_for_account(account_id, group_id, max_id)

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
        seen_gids: set = set()   # ALL group/channel ids still in dialogs (incl. excluded)
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                gid = dialog.id
                seen_gids.add(gid)
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

        # Remove channels the user has left since the previous sync.
        if seen_gids:
            pruned = await database.prune_account_groups(account_id, seen_gids)
            if pruned:
                logger.info(f"[acc {account_id}] Left {pruned} channel(s) removed from tracking")

        s["total_groups"] = len(dialogs)
        s["processed_groups"] = 0
        _refresh_aggregate_status()

        all_new_torrent_ids: List[int] = []
        for gid, entity, name in dialogs:
            s["current_group"] = name
            _refresh_aggregate_status()
            count, torrent_ids = await _sync_group(account_id, client, entity, gid, name)
            link_count = await _sync_group_links(account_id, client, entity, gid, name)
            s["new_files"] += count
            s["new_links"] += link_count
            all_new_torrent_ids.extend(torrent_ids)
            s["processed_groups"] += 1
            _refresh_aggregate_status()
            if count > 0 or link_count > 0:
                logger.info(
                    f"[acc {account_id} {s['processed_groups']}/{s['total_groups']}]"
                    f" {name}: {count} new files, {link_count} new links"
                )

        if all_new_torrent_ids:
            logger.info(f"[acc {account_id}] Auto-parsing {len(all_new_torrent_ids)} new torrent(s)")
            asyncio.create_task(_auto_parse_torrents(all_new_torrent_ids))

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
            new_matches, watch_payload = await database.check_watches()
            if new_matches > 0:
                logger.info(f"Watch terms matched {new_matches} new file(s)")
            await _push_watch_notifications(watch_payload)
        except Exception as e:
            logger.warning(f"Watch check failed: {e}")
        _refresh_aggregate_status()


def _fmt_size_h(n: int) -> str:
    n = int(n or 0)
    if n >= 1 << 40: return f"{n / (1 << 40):.1f} TB"
    if n >= 1 << 30: return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20: return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10: return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


def _file_telegram_link(row: Dict) -> str:
    """Build the Telegram deep-link to the message that holds this file."""
    msg_id = row.get("message_id")
    if not msg_id:
        return ""
    uname = row.get("group_username")
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    gid = row.get("group_id")
    if gid is None:
        return ""
    gid_str = str(gid)
    if gid_str.startswith("-100"):
        gid_str = gid_str[4:]
    elif gid_str.startswith("-"):
        gid_str = gid_str[1:]
    return f"https://t.me/c/{gid_str}/{msg_id}"


async def _push_watch_notifications(watch_payload: List[Dict]):
    """Push each watch match as a single message into the user's own Saved
    Messages chat (the Telethon "me" peer). Honors the global toggle in
    `notify_settings.tg_push_enabled` and skips silently when nothing matched.
    Capped at 10 files per push to keep messages digestible — overflow gets a
    "+N daha" footer."""
    if not watch_payload:
        return
    try:
        settings = await database.get_notify_settings()
    except Exception as e:
        logger.warning(f"[notify] get_notify_settings failed: {e}")
        return
    if not settings.get("tg_push_enabled"):
        return
    try:
        accounts = await database.list_accounts()
    except Exception as e:
        logger.warning(f"[notify] list_accounts failed: {e}")
        return
    active = [a for a in accounts if a.get("is_active", True)]
    if not active:
        return

    # One bulk fetch for all file ids across all watches.
    all_ids: List[int] = []
    for m in watch_payload:
        all_ids.extend(m.get("file_ids") or [])
    if not all_ids:
        return
    rows = await database.get_files_for_notification(all_ids)
    by_id = {int(r["id"]): r for r in rows}

    CAP = 10  # files per push
    for m in watch_payload:
        ids = m.get("file_ids") or []
        if not ids:
            continue
        kw = (m.get("keywords") or "").strip()
        shown = ids[:CAP]
        extra = len(ids) - len(shown)
        lines: List[str] = []
        for fid in shown:
            f = by_id.get(int(fid))
            if not f:
                continue
            fname = (f.get("file_name") or f"#{fid}").strip()
            gname = (f.get("group_name") or "?").strip()
            size  = _fmt_size_h(f.get("file_size") or 0)
            link  = _file_telegram_link(f)
            piece = f"📁 {fname} · {size}\n📡 {gname}"
            if link:
                piece += f"\n🔗 {link}"
            lines.append(piece)
        if not lines:
            continue
        body = f"🎯 İzlem eşleşmesi: {kw}\n\n" + "\n\n".join(lines)
        if extra > 0:
            body += f"\n\n…ve {extra} dosya daha"

        # Send via every active account's session into ITS OWN Saved Messages.
        # We don't deduplicate across accounts on purpose: each user wants
        # their own alerts on their own device.
        for acc in active:
            try:
                client = await get_client(acc["id"])
                if not client.is_connected():
                    await client.connect()
                if not await client.is_user_authorized():
                    continue
                await client.send_message("me", body, link_preview=False)
            except Exception as e:
                logger.warning(
                    f"[notify] send_message failed for account {acc['id']}: {e}"
                )
    try:
        await database.set_notify_settings(last_push_at=datetime.utcnow())
    except Exception:
        pass


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
                file_id = await database.insert_file(
                    group_id=gid,
                    message_id=msg.id,
                    **info,
                    date=date_str,
                    discovered_by_account_id=account_id,
                )
                if file_id is not None:
                    logger.info(f"[realtime acc {account_id}] {name}: {info['file_name']}")
                    if info.get("file_ext") == "torrent":
                        asyncio.create_task(_auto_parse_torrents([file_id]))

        context = (msg.text or getattr(msg, "caption", None) or "")[:300]
        platform_urls = _extract_platform_urls(msg)
        any_link_inserted = False

        for platform, url in platform_urls:
            link_data = _magnet_link_data(url) if platform == "Magnet" else {}
            inserted = await database.insert_link(
                group_id=gid,
                message_id=msg.id,
                platform=platform,
                url=url,
                context=context,
                date=date_str,
                discovered_by_account_id=account_id,
                **link_data,
            )
            if inserted:
                any_link_inserted = True

        # Also catch plain-text magnet links not covered by URL entity detection
        entity_urls = {url for _, url in platform_urls}
        for uri in _extract_all_magnet_urls(msg):
            if uri in entity_urls:
                continue
            link_data = _magnet_link_data(uri)
            inserted = await database.insert_link(
                group_id=gid,
                message_id=msg.id,
                platform="Magnet",
                url=uri,
                context=context,
                date=date_str,
                discovered_by_account_id=account_id,
                **link_data,
            )
            if inserted:
                any_link_inserted = True

        if any_link_inserted:
            logger.info(f"[realtime acc {account_id}] {name}: link(s) indexed")


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


# ---------------------------------------------------------------------------
# Magnet link historical backfill
# ---------------------------------------------------------------------------

magnet_backfill_status: dict = {
    "running": False,
    "total_groups": 0,
    "done_groups": 0,
    "current_group": None,
    "new_magnets": 0,
    # Phase 2 (file-list metadata fetch via aria2c)
    "enrich_phase": False,        # True while we're in the metadata pass
    "enrich_total": 0,            # how many magnets queued for enrichment
    "enrich_done": 0,             # processed (success + fail)
    "enrich_success": 0,          # got a real file list
    "enrich_fail": 0,             # timeout / no peers / parse error
    "current_magnet": None,       # truncated URI of currently-enriching magnet
    "error": None,
}
_magnet_backfill_task: Optional[asyncio.Task] = None


def kick_magnet_backfill() -> bool:
    global _magnet_backfill_task
    if magnet_backfill_status.get("running"):
        return False
    _magnet_backfill_task = asyncio.create_task(run_magnet_backfill())
    return True


async def _backfill_group_magnets(account_id: int, client, group_id, group_name: str):
    try:
        entity = await client.get_input_entity(int(group_id))
    except Exception as e:
        logger.warning(f"[backfill] Cannot resolve {group_id}: {e}")
        return
    async for message in client.iter_messages(entity, search="magnet:", reverse=True):
        if not magnet_backfill_status.get("running"):
            return
        for uri in _extract_all_magnet_urls(message):
            link_data = _magnet_link_data(uri)
            date_str = message.date.isoformat() if message.date else datetime.utcnow().isoformat()
            context = (message.text or getattr(message, "caption", None) or "")[:300]
            inserted = await database.insert_link(
                group_id=group_id,
                message_id=message.id,
                platform="Magnet",
                url=uri,
                context=context,
                date=date_str,
                discovered_by_account_id=account_id,
                **link_data,
            )
            if inserted:
                magnet_backfill_status["new_magnets"] += 1


async def _enrich_magnet_metadata_pass(per_link_timeout: int = 60, max_links: int = 500):
    """Second pass: walk magnet links that still have only the magnet's display
    name and try to pull the real file list via aria2c (DHT + trackers + ut_metadata).

    Sequential, per-link timeout. Honors the global cancel flag so the user
    can stop the run from the UI."""
    import magnet_metadata
    pending = await database.list_magnet_links_needing_enrich(limit=max_links)
    magnet_backfill_status["enrich_phase"]   = True
    magnet_backfill_status["enrich_total"]   = len(pending)
    magnet_backfill_status["enrich_done"]    = 0
    magnet_backfill_status["enrich_success"] = 0
    magnet_backfill_status["enrich_fail"]    = 0
    logger.info(f"[backfill] enrichment pass: {len(pending)} magnet(s) queued")
    for row in pending:
        if not magnet_backfill_status.get("running"):
            return
        magnet_backfill_status["current_magnet"] = (row["url"] or "")[:80]
        try:
            meta = await magnet_metadata.fetch_magnet_metadata(row["url"], timeout=per_link_timeout)
        except Exception as e:
            meta = None
            logger.warning(f"[backfill enrich] fetch error: {e}")
        if meta and meta.get("file_count"):
            files = magnet_metadata.magnet_to_link_files(meta)
            try:
                await database.record_probe_result(
                    link_id=row["id"],
                    available=True,
                    files=files,
                    error=None,
                )
                magnet_backfill_status["enrich_success"] += 1
            except Exception as e:
                logger.warning(f"[backfill enrich] db update failed for link {row['id']}: {e}")
                magnet_backfill_status["enrich_fail"] += 1
        else:
            # No metadata available (DHT couldn't find peers / aria2c timeout)
            # → drop the row entirely. User wants the Links grid to contain
            # only magnets whose file list we can actually display; sticky
            # `magnet-enrich:no-metadata` placeholders are no longer kept.
            try:
                await database._exec("DELETE FROM links WHERE id = $1", row["id"])
            except Exception as e:
                logger.warning(f"[backfill enrich] delete failed for link {row['id']}: {e}")
            magnet_backfill_status["enrich_fail"] += 1
        magnet_backfill_status["enrich_done"] += 1


async def run_magnet_backfill():
    magnet_backfill_status.update({
        "running": True,
        "done_groups": 0,
        "new_magnets": 0,
        "enrich_phase": False,
        "enrich_total": 0,
        "enrich_done": 0,
        "enrich_success": 0,
        "enrich_fail": 0,
        "current_magnet": None,
        "error": None,
        "current_group": None,
    })
    try:
        accounts = await database.list_accounts()
        active = [a for a in accounts if a.get("is_active", True)]
        total = 0
        for a in active:
            groups = await database.get_groups_for_account(a["id"])
            excluded = set(await database.get_excluded_group_ids_for_account(a["id"]))
            total += sum(1 for g in groups if g["id"] not in excluded)
        magnet_backfill_status["total_groups"] = total

        for a in active:
            client = await get_client(a["id"])
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                continue
            groups = await database.get_groups_for_account(a["id"])
            excluded = set(await database.get_excluded_group_ids_for_account(a["id"]))
            for group in groups:
                gid = group["id"]
                if gid in excluded:
                    continue
                name = group.get("display_name") or group.get("name") or str(gid)
                magnet_backfill_status["current_group"] = name
                try:
                    await _backfill_group_magnets(a["id"], client, gid, name)
                except Exception as e:
                    logger.warning(f"[backfill] '{name}': {e}")
                magnet_backfill_status["done_groups"] += 1

        # Phase 2: enrich magnets with real file listings via aria2c+DHT
        if magnet_backfill_status.get("running"):
            await _enrich_magnet_metadata_pass()
    except Exception as e:
        magnet_backfill_status["error"] = str(e)
        logger.error(f"[backfill] Fatal error: {e}")
    finally:
        magnet_backfill_status["running"] = False
        magnet_backfill_status["enrich_phase"] = False
        magnet_backfill_status["current_group"] = None
        magnet_backfill_status["current_magnet"] = None


# ── Archive scan: bulk inspect archives inside magnet links ────────────────────

archive_scan_status: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "success": 0,
    "fail": 0,
    "skipped": 0,
    "current": None,   # truncated URL of current link
    "error": None,
}
_archive_scan_task: Optional[asyncio.Task] = None


def kick_archive_scan() -> bool:
    global _archive_scan_task
    if archive_scan_status.get("running"):
        return False
    _archive_scan_task = asyncio.create_task(run_archive_scan())
    return True


def cancel_archive_scan() -> bool:
    if not archive_scan_status.get("running"):
        return False
    archive_scan_status["running"] = False
    return True


async def run_archive_scan(per_link_timeout: int = 150, concurrency: int = 2):
    """Walk all magnet links that have uninspected ZIP/RAR/7z files and
    partially download their archive headers to list the contents.

    Runs at concurrency=2 to avoid hammering the DHT.
    Respects the running flag so cancel_archive_scan() stops it cleanly."""
    import archive_inspector

    archive_scan_status.update({
        "running": True,
        "total": 0,
        "done": 0,
        "success": 0,
        "fail": 0,
        "skipped": 0,
        "current": None,
        "error": None,
    })

    try:
        pending = await database._q("""
            SELECT l.id, l.url
            FROM links l
            WHERE l.platform = 'Magnet'
              AND l.files_json IS NOT NULL
              AND jsonb_array_length(l.files_json) > 0
              AND l.available = true
              AND NOT EXISTS (
                  SELECT 1 FROM link_archive_contents lac WHERE lac.link_id = l.id
              )
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(l.files_json) AS f
                  WHERE (f->>'name') ~* '\\.(zip|rar|7z)$'
                    AND (f->>'size') IS NOT NULL
                    AND (f->>'size')::bigint BETWEEN 1 AND 524288000
              )
            ORDER BY l.id DESC
        """)

        archive_scan_status["total"] = len(pending)
        logger.info("[archive_scan] %d magnet link(s) with uninspected archives", len(pending))

        sem = asyncio.Semaphore(concurrency)

        async def _process_one(row):
            async with sem:
                if not archive_scan_status.get("running"):
                    archive_scan_status["skipped"] += 1
                    return
                lid = row["id"]
                url = row["url"] or ""
                archive_scan_status["current"] = url[:80]
                try:
                    results = await archive_inspector.inspect_magnet_archives(
                        url, timeout=per_link_timeout
                    )
                    if results:
                        for path, contents in results.items():
                            await database.store_link_archive_contents(lid, path, contents)
                        archive_scan_status["success"] += 1
                        logger.info("[archive_scan] link %d: %d archive(s) inspected", lid, len(results))
                    else:
                        archive_scan_status["fail"] += 1
                except Exception as exc:
                    logger.warning("[archive_scan] link %d error: %s", lid, exc)
                    archive_scan_status["fail"] += 1
                finally:
                    archive_scan_status["done"] += 1

        await asyncio.gather(*[_process_one(r) for r in pending])

    except Exception as exc:
        archive_scan_status["error"] = str(exc)
        logger.error("[archive_scan] Fatal: %s", exc)
    finally:
        archive_scan_status["running"] = False
        archive_scan_status["current"] = None
