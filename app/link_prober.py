"""Visit a file-host URL and report back which file(s) it serves.

Each provider has a small probe function returning a uniform shape:

    {
      "available": True|False|None,   # True=files found, False=dead/inaccessible,
                                      # None=we don't (yet) know how to probe this provider
      "files":     [{"name": str, "size": int}],
      "error":     None | str,
    }

Designed to fail soft — any single provider's parsing error is caught and
turned into available=False (or None for unsupported) so a bad page never
takes down the worker. Where a provider exposes a JSON API (Yandex Disk,
Pixeldrain, Gofile, Mega's API), we use that. Otherwise we scrape minimal
HTML signals (og:title, page <title>, common file-info markers).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

logger = logging.getLogger("link_prober")

_TIMEOUT = aiohttp.ClientTimeout(total=20, sock_connect=8, sock_read=12)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en,tr;q=0.8",
}


def _shape(available: Optional[bool], files: List[Dict] = None, error: str = None) -> Dict:
    return {
        "available": available,
        "files": files or [],
        "error": error,
    }


# ── Yandex Disk ──────────────────────────────────────────────────────────────
async def _probe_yandex(session: aiohttp.ClientSession, url: str) -> Dict:
    api = (
        "https://cloud-api.yandex.net/v1/disk/public/resources"
        f"?public_key={urllib.parse.quote(url, safe='')}&limit=200"
    )
    async with session.get(api) as r:
        if r.status == 404:
            return _shape(False, error="not found")
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        d = await r.json()
    if d.get("type") == "file":
        return _shape(True, [{"name": d.get("name"), "size": d.get("size") or 0}])
    items = (d.get("_embedded") or {}).get("items") or []
    files = [
        {"name": it.get("name"), "size": it.get("size") or 0}
        for it in items
        if it.get("type") == "file"
    ]
    return _shape(bool(files), files)


# ── Pixeldrain ───────────────────────────────────────────────────────────────
_PXD_FILE_RE = re.compile(r"/u/([A-Za-z0-9]+)")
_PXD_LIST_RE = re.compile(r"/l/([A-Za-z0-9]+)")


async def _probe_pixeldrain(session: aiohttp.ClientSession, url: str) -> Dict:
    m = _PXD_LIST_RE.search(url)
    if m:
        api = f"https://pixeldrain.com/api/list/{m.group(1)}"
        async with session.get(api) as r:
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            d = await r.json()
        if not d.get("success", True):
            return _shape(False, error=d.get("message"))
        files = [
            {"name": f.get("name"), "size": f.get("size") or 0}
            for f in (d.get("files") or [])
        ]
        return _shape(bool(files), files)
    m = _PXD_FILE_RE.search(url)
    if not m:
        return _shape(None, error="unrecognized pixeldrain url")
    api = f"https://pixeldrain.com/api/file/{m.group(1)}/info"
    async with session.get(api) as r:
        if r.status == 404:
            return _shape(False, error="not found")
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        d = await r.json()
    if not d.get("success", True):
        return _shape(False, error=d.get("message"))
    return _shape(True, [{"name": d.get("name"), "size": d.get("size") or 0}])


# ── Gofile ───────────────────────────────────────────────────────────────────
_GOFILE_RE = re.compile(r"/d/([A-Za-z0-9]+)")


async def _probe_gofile(session: aiohttp.ClientSession, url: str) -> Dict:
    m = _GOFILE_RE.search(url)
    if not m:
        return _shape(None, error="unrecognized gofile url")
    code = m.group(1)
    # Gofile requires a guest token first
    async with session.post("https://api.gofile.io/accounts") as r:
        if r.status != 200:
            return _shape(False, error=f"token HTTP {r.status}")
        td = await r.json()
    token = (td.get("data") or {}).get("token")
    if not token:
        return _shape(False, error="no guest token")
    api = f"https://api.gofile.io/contents/{code}?wt=4fd6sg89d7s6"
    async with session.get(api, headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        d = await r.json()
    data = d.get("data") or {}
    if d.get("status") != "ok" or not data:
        return _shape(False, error=d.get("status") or "no data")
    children = data.get("children") or {}
    files = []
    for v in children.values():
        if v.get("type") == "file":
            files.append({"name": v.get("name"), "size": v.get("size") or 0})
    return _shape(bool(files), files)


# ── MediaFire (HTML scrape) ──────────────────────────────────────────────────
async def _probe_mediafire(session: aiohttp.ClientSession, url: str) -> Dict:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    low = html.lower()
    if any(s in low for s in (
        "file or folder not found",
        "invalid or deleted file",
        "removed for violation",
        "this content was removed",
    )):
        return _shape(False, error="not found")
    # Filename — try the strongest signals first, fall back to <title>.
    # MediaFire layout has changed several times; this covers 2021-2026 markup.
    fname = (
        _re1(html, r'<div[^>]+class="dl-btn-label"[^>]*title="([^"]+)"')
        or _re1(html, r'class="filename"[^>]*>\s*([^<]+?)\s*<')
        or _re1(html, r'<input[^>]+name="filename"[^>]+value="([^"]+)"')
        or _re1(html, r'aria-label="Download[^"]*"[^>]+title="([^"]+)"')
        or _meta(html, "og:title")
        or _re1(html, r'<title>([^<]+)</title>')
    )
    if fname:
        fname = re.sub(r"\s*[-|]\s*[Mm]edia[Ff]ire.*$", "", fname).strip()
    fsize = 0
    for pat in (
        r'aria-label="Download file"[^>]*>\s*<i[^>]*></i>\s*Download[^<]*\(([^)]+)\)',
        r'class="DLExtraInfo-Item">\s*<span[^>]*>([0-9.,]+\s*[KMGT]?B)\s*</span>',
        # New (2024+) JSON-island markup
        r'"size_pretty"\s*:\s*"([0-9.,]+\s*[KMGT]?B)"',
        r'"file_size"\s*:\s*"?(\d+)"?',
        r'>\s*Size:\s*</[^>]+>\s*<[^>]*>\s*([0-9.,]+\s*[KMGT]?B)\s*<',
    ):
        m = re.search(pat, html, re.I)
        if m:
            g = m.group(1)
            fsize = int(g) if g.isdigit() else _parse_size(g)
            if fsize:
                break
    if not fname or fname.lower().startswith("mediafire"):
        return _shape(False, error="no name")
    return _shape(True, [{"name": fname, "size": fsize}])


# ── Google Drive ─────────────────────────────────────────────────────────────
# Files: HTML scrape of the viewer page (og:title / <title>).
# Folders: server-rendered embed view at /embeddedfolderview?id=ID — no API
# key, no JS, no auth. Lists entries with name + per-file link; size is not
# in the embed view so we record only names (matches user expectation of
# "kısmi de olsa dosya adı").
_GDRIVE_FOLDER_RE  = re.compile(r"/(?:drive/)?(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})")
_GDRIVE_OPENID_RE  = re.compile(r"/open\?id=([A-Za-z0-9_-]{10,})")
_GDRIVE_MAX_ENTRIES = 300


async def _gdrive_id_size(session: aiohttp.ClientSession, file_id: str):
    """drive.usercontent'e Range 0-0 → (Content-Disposition adı, Content-Range boyutu).
    Küçük+büyük tüm indirilebilir dosyalarda tam byte boyut verir; native Doküman/Form
    ve erişilemez dosyalarda (None, 0) döner."""
    name, size = None, 0
    try:
        dl = ("https://drive.usercontent.google.com/download"
              f"?id={file_id}&export=download&confirm=t")
        async with session.get(dl, headers={"Range": "bytes=0-0"},
                               allow_redirects=True) as r:
            cr = r.headers.get("Content-Range") or ""
            tot = cr.rsplit("/", 1)[-1] if "/" in cr else ""
            if tot.isdigit():
                size = int(tot)
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'''filename\*?=(?:UTF-8'')?"?([^";]+)''', cd)
            if m:
                dn = m.group(1).strip().strip('"')
                if dn:
                    name = dn
    except Exception:
        pass
    return name, size


async def _probe_gdrive_folder(session: aiohttp.ClientSession,
                                folder_id: str) -> Dict:
    embed = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    try:
        async with session.get(embed, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="folder not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    low = html.lower()
    if "sorry, the file you have requested does not exist" in low or "access denied" in low:
        return _shape(False, error="folder not accessible")
    # Pair each entry's link kind (file vs sub-folder) with its title in
    # document order. Embed view emits one `<a>` then one `flip-entry-title`
    # per entry; zipping preserves the pairing.
    hrefs = re.findall(
        r'<a href="https?://drive\.google\.com/(file/d|drive/folders|folder)/([A-Za-z0-9_-]+)',
        html,
    )
    titles = re.findall(r'class="flip-entry-title"[^>]*>\s*([^<]+?)\s*<', html)
    entries: List[Tuple[str, str]] = []   # (title, file_id)
    for (kind_path, fid), title in zip(hrefs, titles):
        if kind_path != "file/d":
            continue   # skip sub-folders (would need recursion + budget)
        name = title.strip()
        if name:
            entries.append((name, fid))
        if len(entries) >= _GDRIVE_MAX_ENTRIES:
            break
    if not entries:
        return _shape(False, error="empty or restricted folder")
    # İç dosya boyutlarını Range ile çek (embed HTML boyut vermiyor). Eşzamanlılık
    # sınırlı tutulur; probe_one timeout'u bu iş için 60s'e yükseltildi.
    _sem = asyncio.Semaphore(8)

    async def _sized(title: str, fid: str) -> Dict:
        async with _sem:
            dn, sz = await _gdrive_id_size(session, fid)
            return {"name": dn or title, "size": sz}

    files = await asyncio.gather(*[_sized(t, f) for t, f in entries])
    return _shape(True, list(files))


async def _probe_gdrive(session: aiohttp.ClientSession, url: str) -> Dict:
    fm = _GDRIVE_FOLDER_RE.search(url)
    if fm:
        return await _probe_gdrive_folder(session, fm.group(1))
    om = _GDRIVE_OPENID_RE.search(url)
    if om:
        # `open?id=X` can resolve to either a file or a folder. Try folder
        # embed first (cheap, one GET). If it returns a real listing, we're
        # done. Otherwise, fall through to the file probe path against the
        # equivalent /file/d/ URL — same total cost as before for non-folders.
        res = await _probe_gdrive_folder(session, om.group(1))
        if res.get("available"):
            return res
        url = f"https://drive.google.com/file/d/{om.group(1)}/view"
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    # Sign-in / no permission walls
    walls = (
        "You need access",
        "Request access",
        "Sign in",
        "Sign in to continue",
        "404. That's an error",
        "Page not found",
    )
    if any(w in html for w in walls) and "drive_module" not in html:
        # The viewer page often includes "Sign in" boilerplate; only bail if no
        # actual file content marker is present.
        if 'itemprop="name"' not in html and 'og:title' not in html:
            return _shape(False, error="access denied")
    # File name from <title> or og:title
    name = _meta(html, "og:title") or _re1(html, r"<title>([^<]+)</title>")
    if name:
        name = name.replace(" - Google Drive", "").strip()
    if not name or name in ("Sign in", "Google Drive", "Page not found"):
        return _shape(False, error="no metadata")
    # Boyut (ve uzantılı gerçek ad) için ortak Range yardımcısıyla drive.usercontent'i sorgula.
    # Native Doküman/Form indirilemez → Content-Range gelmez → boyut 0 kalır (doğru).
    size = 0
    fm2 = re.search(r'/file/d/([A-Za-z0-9_-]+)', url) or _GDRIVE_OPENID_RE.search(url)
    if fm2:
        dn, size = await _gdrive_id_size(session, fm2.group(1))
        if dn:
            name = dn
    return _shape(True, [{"name": name, "size": size}])


# ── Dropbox ──────────────────────────────────────────────────────────────────
async def _probe_dropbox(session: aiohttp.ClientSession, url: str) -> Dict:
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        html = await r.text(errors="replace")
    if "We can't seem to find" in html or "This shared link doesn't exist" in html:
        return _shape(False, error="not found")
    name = _meta(html, "og:title") or _re1(html, r"<title>([^<]+)</title>")
    if name:
        name = re.sub(r"\s*[-–]\s*Dropbox$", "", name).strip()
    if not name:
        return _shape(False, error="no metadata")
    return _shape(True, [{"name": name, "size": 0}])


# ── Mega ─────────────────────────────────────────────────────────────────────
# ── Mega.nz decryption ───────────────────────────────────────────────────────
# Mega's "share key" is part of the URL hash fragment (the bit after `#`), so
# anyone with the link can derive the AES key needed to decrypt the file's
# attribute blob (which carries the filename). The Mega web client does this
# locally; we replicate the same crypto here so we can record the real
# filename in the link DB instead of just a "(şifreli — Mega)" placeholder.
#
# File link:    https://mega.nz/file/HANDLE#KEY32   (KEY32 = 32-byte b64url)
# Legacy file:  https://mega.nz/#!HANDLE!KEY32
# Folder link:  https://mega.nz/folder/HANDLE#KEY16 (KEY16 = 16-byte master)
# Legacy folder:https://mega.nz/#F!HANDLE!KEY16
#
# All Mega API calls go through a single shared lock + min-interval gate so
# we never burst on their rate limiter.

_MEGA_LOCK = asyncio.Lock()
_MEGA_LAST_REQ_AT = 0.0
_MEGA_MIN_GAP_SEC = 1.5         # gap between API calls
_MEGA_MAX_FOLDER_FILES = 200    # safety cap on folder listings


def _b64url_decode(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _mega_parse_url(url: str) -> Optional[Tuple[str, str, str]]:
    """Return ('file'|'folder', handle, key_b64) or None if unparseable."""
    m = re.search(r"mega\.nz/(file|folder)/([^/#?]+)#([A-Za-z0-9_\-]+)", url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.search(r"mega\.nz/#(F?)!([^!]+)!([A-Za-z0-9_\-]+)", url)
    if m:
        return ("folder" if m.group(1) == "F" else "file"), m.group(2), m.group(3)
    return None


def _mega_file_aes_key(key_b64: str) -> bytes:
    """Derive the AES-128 key from a 32-byte file share key.
    aes_key[i] = key[i] XOR key[i+4] over four 32-bit ints."""
    raw = _b64url_decode(key_b64)
    if len(raw) != 32:
        return b""
    a = struct.unpack(">8I", raw)
    return struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])


def _aes_cbc_decrypt(key: bytes, ct: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16),
                    backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(ct) + dec.finalize()


def _aes_ecb_decrypt(key: bytes, ct: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(),
                    backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(ct) + dec.finalize()


def _mega_decrypt_attr(attr_b64: str, aes_key: bytes) -> Optional[Dict]:
    """Decode + AES-decrypt the `at` attribute blob → parsed JSON dict."""
    if not _HAS_CRYPTO or not aes_key or not attr_b64:
        return None
    try:
        ct = _b64url_decode(attr_b64)
    except Exception:
        return None
    if not ct or len(ct) % 16:
        return None
    try:
        pt = _aes_cbc_decrypt(aes_key, ct).rstrip(b"\x00")
    except Exception:
        return None
    if not pt.startswith(b"MEGA{"):
        return None
    try:
        return json.loads(pt[4:].decode("utf-8", errors="ignore"))
    except Exception:
        return None


async def _mega_call_api(session: aiohttp.ClientSession,
                          payload: List[Dict],
                          folder_handle: Optional[str] = None) -> Any:
    """Single-flight POST to Mega's `cs` endpoint with rate limiting."""
    global _MEGA_LAST_REQ_AT
    async with _MEGA_LOCK:
        gap = time.time() - _MEGA_LAST_REQ_AT
        if gap < _MEGA_MIN_GAP_SEC:
            await asyncio.sleep(_MEGA_MIN_GAP_SEC - gap)
        suffix = f"?n={folder_handle}" if folder_handle else ""
        try:
            async with session.post(
                f"https://g.api.mega.co.nz/cs{suffix}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                d = await r.json(content_type=None)
        finally:
            _MEGA_LAST_REQ_AT = time.time()
    return d


async def _probe_mega_file(session: aiohttp.ClientSession,
                            handle: str, key_b64: str) -> Dict:
    try:
        d = await _mega_call_api(session, [{"a": "g", "g": 1, "p": handle}])
    except Exception as e:
        return _shape(None, error=f"mega api: {e}")
    if not isinstance(d, list) or not d:
        return _shape(None, error="unexpected mega response")
    first = d[0]
    if isinstance(first, int) and first < 0:
        return _shape(False, error=f"mega code {first}")
    if not isinstance(first, dict):
        return _shape(None, error="unexpected mega response")

    size = int(first.get("s") or 0)
    at_b64 = first.get("at") or ""
    aes_key = _mega_file_aes_key(key_b64)
    decoded = _mega_decrypt_attr(at_b64, aes_key)
    name = (decoded or {}).get("n") if decoded else None
    if name:
        return _shape(True, [{"name": name, "size": size}])
    # Decryption failed (bad key length, missing crypto lib, etc.). Record a
    # stable probe_error so the startup re-probe migration knows not to
    # re-queue this link forever — but still mark available so the link
    # isn't shown as dead.
    return _shape(True, [{"name": "(şifreli — Mega)", "size": size}],
                  error="mega:decrypt-failed")


async def _probe_mega_folder(session: aiohttp.ClientSession,
                              folder_handle: str, key_b64: str) -> Dict:
    if not _HAS_CRYPTO:
        return _shape(None, error="crypto unavailable — install `cryptography`")
    try:
        master_key = _b64url_decode(key_b64)
    except Exception:
        return _shape(None, error="bad folder key")
    if len(master_key) != 16:
        return _shape(None, error=f"folder key wrong length ({len(master_key)})")

    try:
        d = await _mega_call_api(
            session, [{"a": "f", "c": 1, "r": 1, "ca": 1}],
            folder_handle=folder_handle,
        )
    except Exception as e:
        return _shape(None, error=f"mega folder api: {e}")

    if isinstance(d, list) and d and isinstance(d[0], int) and d[0] < 0:
        return _shape(False, error=f"mega folder code {d[0]}")
    if not (isinstance(d, list) and d and isinstance(d[0], dict)):
        return _shape(None, error="unexpected mega folder response")

    nodes = d[0].get("f") or []
    files: List[Dict] = []
    for n in nodes[: _MEGA_MAX_FOLDER_FILES * 5]:  # walk a bit past cap for type 0 filter
        if n.get("t") != 0:  # 0 = file
            continue
        k_field = n.get("k") or ""
        if not k_field:
            continue
        # k may carry MULTIPLE shares separated by `/`, each "owner:enc_key".
        # Only one of those is encrypted with the master key we have — the
        # others belong to other owners/folders. Try each segment until one
        # decrypts to a valid `MEGA{` attribute blob.
        name: Optional[str] = None
        for seg in k_field.split("/"):
            if ":" in seg:
                enc_key_b64 = seg.split(":", 1)[1]
            else:
                enc_key_b64 = seg
            try:
                enc_key = _b64url_decode(enc_key_b64)
                if len(enc_key) != 32:
                    continue
                raw_key = _aes_ecb_decrypt(master_key, enc_key)
                if len(raw_key) != 32:
                    continue
                ints = struct.unpack(">8I", raw_key)
                aes_key = struct.pack(
                    ">4I",
                    ints[0] ^ ints[4], ints[1] ^ ints[5],
                    ints[2] ^ ints[6], ints[3] ^ ints[7],
                )
                attr = _mega_decrypt_attr(n.get("a") or "", aes_key)
                if attr and attr.get("n"):
                    name = attr["n"]
                    break
            except Exception:
                continue
        files.append({
            "name": name or "(şifreli)",
            "size": int(n.get("s") or 0),
        })
        if len(files) >= _MEGA_MAX_FOLDER_FILES:
            break
    if not files:
        return _shape(False, error="no files in folder")
    return _shape(True, files)


async def _probe_mega(session: aiohttp.ClientSession, url: str) -> Dict:
    parsed = _mega_parse_url(url)
    if not parsed:
        return _shape(None, error="unrecognized mega url")
    kind, handle, key_b64 = parsed
    if kind == "folder":
        return await _probe_mega_folder(session, handle, key_b64)
    return await _probe_mega_file(session, handle, key_b64)


# ── Cyberdrop ────────────────────────────────────────────────────────────────
# Public JSON API: /api/file/info/{slug} for files, /api/album/{slug} for
# albums. Single GET per link; no auth.
_CYBERDROP_FILE_RE  = re.compile(r"cyberdrop\.me/f/([A-Za-z0-9_-]+)")
_CYBERDROP_ALBUM_RE = re.compile(r"cyberdrop\.me/a/([A-Za-z0-9_-]+)")


async def _probe_cyberdrop(session: aiohttp.ClientSession, url: str) -> Dict:
    m = _CYBERDROP_ALBUM_RE.search(url)
    if m:
        api = f"https://api.cyberdrop.me/api/album/{m.group(1)}"
        try:
            async with session.get(api) as r:
                if r.status == 404:
                    return _shape(False, error="album not found")
                if r.status != 200:
                    return _shape(False, error=f"HTTP {r.status}")
                d = await r.json(content_type=None)
        except Exception as e:
            # Fall back to HTML scrape
            return await _probe_cyberdrop_html(session, url)
        files = [
            {"name": f.get("name") or f.get("filename"),
             "size": int(f.get("size") or 0)}
            for f in (d.get("files") or [])
            if (f.get("name") or f.get("filename"))
        ]
        return _shape(bool(files), files)
    m = _CYBERDROP_FILE_RE.search(url)
    if not m:
        return await _probe_cyberdrop_html(session, url)
    api = f"https://api.cyberdrop.me/api/file/info/{m.group(1)}"
    try:
        async with session.get(api) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return await _probe_cyberdrop_html(session, url)
            d = await r.json(content_type=None)
    except Exception:
        return await _probe_cyberdrop_html(session, url)
    name = d.get("name") or d.get("filename")
    size = int(d.get("size") or 0)
    if not name:
        return _shape(False, error="no name")
    return _shape(True, [{"name": name, "size": size}])


async def _probe_cyberdrop_html(session: aiohttp.ClientSession, url: str) -> Dict:
    """Fallback when the JSON API changes shape — works for files only."""
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    name = _re1(html, r'id="file-title"[^>]*>([^<]+)<') or _meta(html, "og:title")
    fsize = 0
    sm = re.search(r'([0-9.,]+\s*[KMGT]?B)\b', html)
    if sm:
        fsize = _parse_size(sm.group(1))
    if not name:
        return _shape(False, error="no name")
    return _shape(True, [{"name": name.strip(), "size": fsize}])


# ── Bunkr (bunkr.is / bunkr.su / bunkr.la / bunkrr.su / bunkr.cr) ─────────────
# Bunkr keeps rotating its API; the only stable extraction is from the file/
# album HTML page. Both pages embed the filename and size in plain text near
# the download button. Rate-limit-friendly: single GET per link.
_BUNKR_FILE_RE  = re.compile(r"bunkr+\.[a-z]{2,4}/f/([A-Za-z0-9_-]+)")
_BUNKR_ALBUM_RE = re.compile(r"bunkr+\.[a-z]{2,4}/a/([A-Za-z0-9_-]+)")


async def _probe_bunkr(session: aiohttp.ClientSession, url: str) -> Dict:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    if _BUNKR_ALBUM_RE.search(url):
        # Albums list each file as <div class="grid-images_box"> with name +
        # size inside. Bunkr's HTML rotates; try a few patterns.
        files: List[Dict] = []
        # Pattern 1: data-attribute style
        for m in re.finditer(
            r'data-original-name=["\']([^"\']+)["\'][^>]*data-size=["\']?(\d+)',
            html,
        ):
            files.append({"name": m.group(1), "size": int(m.group(2))})
        # Pattern 2: paired name + size in adjacent <p> tags
        if not files:
            for m in re.finditer(
                r'<p[^>]*class="[^"]*truncate[^"]*"[^>]*title="([^"]+)"[^>]*>.*?'
                r'<p[^>]*>([0-9.,]+\s*[KMGT]?B)\s*</p>',
                html, re.S,
            ):
                files.append({"name": m.group(1).strip(),
                              "size": _parse_size(m.group(2))})
        # Pattern 3: filename links
        if not files:
            for m in re.finditer(
                r'href="[^"]+"[^>]*>\s*([^<\s][^<]{1,200}?\.[A-Za-z0-9]{1,5})\s*<',
                html,
            ):
                name = m.group(1).strip()
                if name and "/" not in name and len(name) < 200:
                    files.append({"name": name, "size": 0})
        if not files:
            return _shape(False, error="album: no files parsed")
        # Dedupe by name (some patterns match twice)
        seen = set(); uniq = []
        for f in files:
            if f["name"] in seen: continue
            seen.add(f["name"]); uniq.append(f)
        return _shape(True, uniq[:300])
    # File page
    name = (
        _re1(html, r'<h1[^>]*id="?file-?name"?[^>]*>([^<]+)</h1>')
        or _re1(html, r'property="og:title"[^>]+content="([^"]+)"')
        or _meta(html, "og:title")
        or _re1(html, r'<title>([^<]+)</title>')
    )
    if name:
        name = name.replace(" | Bunkr", "").replace(" - Bunkr", "").strip()
    fsize = 0
    sm = (
        re.search(r'>\s*([0-9.,]+\s*[KMGT]?B)\s*<', html)
        or re.search(r'data-size=["\']?(\d+)', html)
    )
    if sm:
        g = sm.group(1)
        fsize = int(g) if g.isdigit() else _parse_size(g)
    if not name:
        return _shape(False, error="no name")
    return _shape(True, [{"name": name, "size": fsize}])


# ── Krakenfiles ──────────────────────────────────────────────────────────────
# HTML scrape: the file page has the name + size visible near the download
# button. URL shape: https://krakenfiles.com/view/SLUG/file.html
async def _probe_krakenfiles(session: aiohttp.ClientSession, url: str) -> Dict:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    # "File not found" / "deleted" walls
    low = html.lower()
    if any(s in low for s in (
        "file not found",
        "has been deleted",
        "file is not available",
        "the requested url was not found",
    )):
        return _shape(False, error="not found")
    name = (
        _re1(html, r'<input[^>]+name="filename"[^>]+value="([^"]+)"')
        or _re1(html, r'class="file-?name"[^>]*>\s*([^<]+?)\s*<')
        or _re1(html, r'id="file-?name"[^>]*>\s*([^<]+?)\s*<')
        or _re1(html, r'<h[1-3][^>]*class="[^"]*name[^"]*"[^>]*>\s*([^<]+?)\s*<')
        or _re1(html, r'data-file-?name=["\']([^"\']+)["\']')
        or _re1(html, r'"file[_-]?name"\s*:\s*"([^"]+)"')
        or _re1(html, r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"')
        or _re1(html, r'<title>([^<]+)</title>')
    )
    if name:
        name = re.sub(r"\s*[-|]\s*[Kk]raken[Ff]iles.*$", "", name).strip()
    fsize = 0
    for pat in (
        r'class="file-?size"[^>]*>\s*([0-9.,]+\s*[KMGT]?B)\s*<',
        r'>Size:\s*</[^>]+>\s*<[^>]*>\s*([0-9.,]+\s*[KMGT]?B)\s*<',
        r'data-file-?size=["\']?(\d+)',
        r'"file[_-]?size"\s*:\s*"?([0-9.,]+\s*[KMGT]?B|\d+)',
        # Catch-all near the download button
        r'download[^<]*<[^>]*>\s*([0-9.,]+\s*[KMGT]?B)\s*<',
    ):
        sm = re.search(pat, html, re.I)
        if sm:
            g = sm.group(1)
            fsize = int(g) if g.isdigit() else _parse_size(g)
            if fsize:
                break
    if not name or name.lower().startswith("krakenfiles"):
        return _shape(False, error="no name")
    return _shape(True, [{"name": name, "size": fsize}])


# ── Catbox / Litterbox ───────────────────────────────────────────────────────
# Both expose the original filename directly in the URL path; no network call
# needed. Catbox is permanent storage, Litterbox is the 24h/72h temp variant.
# Litterbox also rotates the filename for privacy on some uploads — we still
# get a name even if it's a hash, which beats nothing.
_CATBOX_PATH_RE = re.compile(r"/([^/?#]+\.[A-Za-z0-9]{1,8})\b")


async def _probe_catbox(session: aiohttp.ClientSession, url: str) -> Dict:
    m = _CATBOX_PATH_RE.search(url)
    if not m:
        return _shape(None, error="unrecognized catbox url")
    name = urllib.parse.unquote(m.group(1))
    # Optional liveness check: HEAD the URL. Cheap; lets us mark dead links.
    size = 0
    try:
        async with session.head(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status == 200:
                size = int(r.headers.get("Content-Length") or 0)
    except Exception:
        # Network blip — still trust the URL-derived name.
        pass
    return _shape(True, [{"name": name, "size": size}])


# Litterbox shares the same URL/filename scheme as Catbox.
_probe_litterbox = _probe_catbox


# ── Sendspace ────────────────────────────────────────────────────────────────
# HTML scrape. URL: https://www.sendspace.com/file/HASH or /pro/dl/HASH
async def _probe_sendspace(session: aiohttp.ClientSession, url: str) -> Dict:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e))
    low = html.lower()
    if "file has been deleted" in low or "file does not exist" in low or "file not found" in low:
        return _shape(False, error="not found")
    name = (
        _re1(html, r'<h2[^>]*class="[^"]*bgray[^"]*"[^>]*>\s*([^<]+?)\s*</h2>')
        or _re1(html, r'class="filename"[^>]*>\s*([^<]+?)\s*<')
        or _meta(html, "og:title")
        or _re1(html, r'<title>([^<]+)</title>')
    )
    if name:
        name = re.sub(r"\s*-\s*[Ss]end[Ss]pace.*$", "", name).strip()
    fsize = 0
    for pat in (
        r'File Size:\s*</[^>]+>\s*<[^>]*>\s*([0-9.,]+\s*[KMGT]?B)',
        r'class="bgray"[^>]*>\s*Size:\s*</[^>]+>\s*([0-9.,]+\s*[KMGT]?B)',
        r'>\s*([0-9.,]+\s*[KMGT]?B)\s*<',
    ):
        sm = re.search(pat, html, re.I)
        if sm:
            fsize = _parse_size(sm.group(1))
            if fsize:
                break
    if not name or name.lower().startswith("sendspace"):
        return _shape(False, error="no name")
    return _shape(True, [{"name": name, "size": fsize}])


# ── Magnet URI ───────────────────────────────────────────────────────────────
async def _probe_magnet(session: aiohttp.ClientSession, url: str) -> Dict:
    """Parse magnet URI metadata — no network request needed."""
    from urllib.parse import parse_qs
    if '?' not in url:
        return _shape(False, error="invalid magnet uri")
    qs_str = url.split('?', 1)[1]
    qs = parse_qs(qs_str, keep_blank_values=False)
    xt = (qs.get('xt') or [''])[0]
    infohash = ''
    if 'urn:btih:' in xt.lower():
        raw = xt.lower().split('urn:btih:', 1)[1]
        infohash = raw.split('&')[0].strip()
    if not infohash:
        return _shape(False, error="no infohash in magnet uri")
    name = (qs.get('dn') or [''])[0] or f"Magnet {infohash[:8].upper()}…"
    try:
        size = int((qs.get('xl') or ['0'])[0])
    except (ValueError, TypeError):
        size = 0
    return _shape(True, [{'name': name, 'size': size}])


# ── helpers ──────────────────────────────────────────────────────────────────
def _meta(html: str, prop: str) -> Optional[str]:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    return m.group(1) if m else None


def _re1(html: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, html, re.I | re.S)
    return m.group(1).strip() if m else None


def _parse_size(s: str) -> int:
    s = s.strip().upper().replace(",", ".")
    m = re.match(r"([0-9.]+)\s*([KMGT]?)B?", s)
    if not m:
        return 0
    v = float(m.group(1))
    unit = m.group(2)
    return int(v * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit])


# ── Dispatch ─────────────────────────────────────────────────────────────────
def _clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*[-|–—]\s*(RSLinks|GPLinks|10Drives|DevUploads|APMFile|FTUApps|"
               r"FreeCracks[^<]*|Hide01|ZOOM-PLATFORM|Telegraph|Free Download|Download).*$",
               "", s, flags=re.I)
    return s.strip()[:200]


async def _probe_telegraph(session: aiohttp.ClientSession, url: str) -> Dict:
    """telegra.ph/file/<hash>.<ext> — URL doğrudan dosya; ad yoldan, boyut Content-Length'ten."""
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "telegraph-file"
    size = 0
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status != 200:
                return _shape(False, error=f"HTTP {r.status}")
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                size = int(cl)
    except Exception:
        return _shape(True, [{"name": name, "size": 0}])
    return _shape(True, [{"name": name, "size": size}])


async def _probe_generic(session: aiohttp.ClientSession, url: str) -> Dict:
    """Best-effort genel prober. Doğrudan dosya ise Content-Disposition/Length'ten
    ad+boyut; HTML sayfa ise dosya-adı kalıpları / og:title / <title>. Link-kilitleyici
    ve katalog sayfaları için çoğunlukla sayfa başlığını döndürür; metadata çıkmazsa
    available=None (link yine de yakalı kalır)."""
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status == 404:
                return _shape(False, error="not found")
            if r.status >= 400:
                return _shape(False, error=f"HTTP {r.status}")
            ctype = (r.headers.get("Content-Type") or "").lower()
            cdisp = r.headers.get("Content-Disposition") or ""
            clen = r.headers.get("Content-Length")
            if ctype and "html" not in ctype and "text/plain" not in ctype:
                name = (_re1(cdisp, r'filename\*?=(?:UTF-8\'\')?"?([^";]+)')
                        or url.split("?")[0].rstrip("/").split("/")[-1] or "file")
                size = int(clen) if (clen and clen.isdigit()) else 0
                return _shape(True, [{"name": _clean_name(name), "size": size}])
            html = await r.text(errors="replace")
    except Exception as e:
        return _shape(False, error=str(e)[:60])
    low = html.lower()
    if any(s in low for s in ("file not found", "has been deleted", "file is not available",
                              "404 not found", "no longer available", "dosya bulunamad",
                              "page not found", "has been removed", "link expired")):
        return _shape(False, error="not found")
    name = (_re1(cdisp, r'filename\*?=(?:UTF-8\'\')?"?([^";]+)')
            or _re1(html, r'<input[^>]+name=["\']fname["\'][^>]+value=["\']([^"\']+)')
            or _re1(html, r'class="[^"]*file-?name[^"]*"[^>]*>\s*([^<]+?)\s*<')
            or _re1(html, r'data-file-?name=["\']([^"\']+)["\']')
            or _re1(html, r'"file[_-]?name"\s*:\s*"([^"]+)"')
            or _re1(html, r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)')
            or _re1(html, r'<title>\s*([^<]+?)\s*</title>'))
    size = 0
    for pat in (r'data-file-?size=["\']?(\d{3,})',
                r'"file[_-]?size"\s*:\s*"?(\d{3,})',
                r'([0-9][0-9.,]*\s*[KMGT]B)\b'):
        sm = re.search(pat, html, re.I)
        if sm:
            g = sm.group(1)
            size = int(g) if g.isdigit() else _parse_size(g)
            if size:
                break
    if not name:
        return _shape(None, error="no metadata")
    return _shape(True, [{"name": _clean_name(name), "size": size}])


_DISPATCH = {
    "Google Drive":   _probe_gdrive,
    "Yandex Disk":    _probe_yandex,
    "Pixeldrain":     _probe_pixeldrain,
    "Gofile":         _probe_gofile,
    "MediaFire":      _probe_mediafire,
    "Dropbox":        _probe_dropbox,
    "Mega":           _probe_mega,
    "Bunkr":          _probe_bunkr,
    "Cyberdrop":      _probe_cyberdrop,
    "Krakenfiles":    _probe_krakenfiles,
    "Catbox":         _probe_catbox,
    "Litterbox":      _probe_litterbox,
    "Sendspace":      _probe_sendspace,
    "Magnet":         _probe_magnet,
    # ── 2026-07: düşük-puanlı kanal taramasıyla eklenen host'lar ──
    "Telegraph":      _probe_telegraph,
    "RSLinks":        _probe_generic,
    "10Drives":       _probe_generic,
    "DevUploads":     _probe_generic,
    "APMFile":        _probe_generic,
    "FTUApps":        _probe_generic,
    "FreeCracks":     _probe_generic,
    "Hide01":         _probe_generic,
    "Zoom-Platform":  _probe_generic,
    "GPLinks":        _probe_generic,
    "Magfi":          _probe_generic,
    "Linktw":         _probe_generic,
}


async def probe_one(
    session: aiohttp.ClientSession, platform: str, url: str
) -> Dict:
    fn = _DISPATCH.get(platform)
    if fn is None:
        return _shape(None, error="unsupported platform")
    try:
        # 60s: gdrive klasörleri iç dosyaların boyutunu Range ile (eşzamanlı) çektiği
        # için tek-dosya (~5s) probe'lardan daha uzun sürebilir.
        return await asyncio.wait_for(fn(session, url), timeout=60)
    except asyncio.TimeoutError:
        return _shape(False, error="timeout")
    except Exception as e:
        return _shape(False, error=f"{type(e).__name__}: {e}")


def make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=_TIMEOUT,
        headers=_HEADERS,
        connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
    )
