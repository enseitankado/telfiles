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
import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import aiohttp

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
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        html = await r.text(errors="replace")
    if "File or Folder Not Found" in html or "Invalid or Deleted File" in html:
        return _shape(False, error="not found")
    name = _meta(html, "og:title")
    fn = _re1(html, r'class="filename">([^<]+)</')
    fname = (fn or name or "").strip()
    fsize = 0
    for pat in (
        r'aria-label="Download file"[^>]*>\s*<i[^>]*></i>\s*Download[^<]*\(([^)]+)\)',
        r'class="DLExtraInfo-Item">\s*<span[^>]*>([0-9.,]+\s*[KMGT]B)</span>',
    ):
        m = re.search(pat, html, re.I)
        if m:
            fsize = _parse_size(m.group(1))
            break
    if not fname:
        return _shape(False, error="no name")
    return _shape(True, [{"name": fname, "size": fsize}])


# ── Google Drive (HTML scrape, file links only) ──────────────────────────────
async def _probe_gdrive(session: aiohttp.ClientSession, url: str) -> Dict:
    if "/folders/" in url or "/drive/folders" in url:
        # Folder pages are JS-rendered, not feasible without a headless browser
        return _shape(None, error="folder")
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200:
            return _shape(False, error=f"HTTP {r.status}")
        html = await r.text(errors="replace")
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
    return _shape(True, [{"name": name, "size": 0}])


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
async def _probe_mega(session: aiohttp.ClientSession, url: str) -> Dict:
    # Mega is end-to-end encrypted client-side; the public REST endpoint can
    # still tell us whether the link exists. We send `g:1` to ask only for
    # metadata (encrypted) — we can't decrypt, but a non-error response means
    # the link is live.
    m = re.search(r"/file/([^/#?]+)", url) or re.search(r"#!([^!]+)!", url)
    if not m:
        return _shape(None, error="unrecognized mega url")
    file_handle = m.group(1)
    try:
        async with session.post(
            "https://g.api.mega.co.nz/cs",
            json=[{"a": "g", "g": 1, "p": file_handle}],
        ) as r:
            d = await r.json()
    except Exception as e:
        return _shape(None, error=str(e))
    # API returns either [{...meta...}] for existing link or a negative int
    # like [-9] for "not found"
    if isinstance(d, list) and d:
        first = d[0]
        if isinstance(first, int) and first < 0:
            return _shape(False, error=f"mega code {first}")
        if isinstance(first, dict):
            size = first.get("s") or 0
            # Filename is encrypted; we know nothing more without the key
            return _shape(True, [{"name": "(şifreli — Mega)", "size": int(size or 0)}])
    return _shape(None, error="unexpected mega response")


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
_DISPATCH = {
    "Google Drive":   _probe_gdrive,
    "Yandex Disk":    _probe_yandex,
    "Pixeldrain":     _probe_pixeldrain,
    "Gofile":         _probe_gofile,
    "MediaFire":      _probe_mediafire,
    "Dropbox":        _probe_dropbox,
    "Mega":           _probe_mega,
}


async def probe_one(
    session: aiohttp.ClientSession, platform: str, url: str
) -> Dict:
    fn = _DISPATCH.get(platform)
    if fn is None:
        return _shape(None, error="unsupported platform")
    try:
        return await asyncio.wait_for(fn(session, url), timeout=25)
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
