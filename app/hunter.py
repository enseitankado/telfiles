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
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import Channel, InputMessagesFilterDocument, DocumentAttributeFilename, InputPeerChannel

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


def _emit_event(stage: str, msg: str, level: str = "info"):
    """Append to rolling event log; keep only the last 80."""
    try:
        status["events"].append({
            "ts": datetime.utcnow().isoformat(),
            "stage": stage, "level": level, "msg": msg[:240],
        })
        if len(status["events"]) > 80:
            status["events"] = status["events"][-80:]
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

# Cache of discovered URLs per source per session
_DISCOVERY_CACHE: Dict[str, List[str]] = {}

# Public file-host link domains we already track — skip these as channel candidates
_NON_CHANNEL_USERNAMES: Set[str] = {
    "joinchat", "share", "addstickers", "addtheme", "iv", "proxy", "setlanguage",
    "joinforum", "addemoji", "addtopic", "boost",
}

_running_lock = asyncio.Lock()
_run_task: Optional[asyncio.Task] = None


def _normalize_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip().lstrip("@").lower()
    if u in _NON_CHANNEL_USERNAMES:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", u):
        return None
    return u


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
                _emit_event("stage2", f"HTTP {r.status} {short}", "warn")
                return None
            text = await r.text(errors="ignore")
            # Some sites return 200 with a Cloudflare/JS challenge page.
            low = text[:5000].lower()
            if ("cloudflare" in low and ("challenge" in low or "checking your browser" in low))                or "captcha" in low or "are you a robot" in low:
                _emit_event("stage2", f"challenge page on {short}", "warn")
                return None
            return text
    except Exception as e:
        _emit_event("stage2", f"fail {short}: {str(e)[:60]}", "warn")
        return None


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
        _emit_event("stage2", f"{name}: {n} consecutive failures → cool-down 6h", "warn")


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


def _build_search_queries(keywords: List[str], max_q: int = 24) -> List[str]:
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
                _emit_event("stage2", f"{name}: 3 fails in a row, stopping site early", "warn")
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
                                max_q: int = 8) -> Set[str]:
    """Generic search-engine adapter. Warms up via the homepage so cookies are
    set, then issues `max_q` queries with browser-like headers + Referer."""
    found: Set[str] = set()
    # Warm up
    _ = await _fetch_text(session, home_url)
    await asyncio.sleep(delay_ms / 1000)

    consecutive_fails = 0
    for q in queries[:max_q]:
        if _check_interrupt("stage2"): break
        url = query_url_tpl.format(q=aiohttp.helpers.quote(q))
        html = await _fetch_text(session, url, referer=home_url)
        if not html:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                _emit_event("stage2", f"{name}: 3 fails — backing off this run", "warn")
                break
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


async def _stage2_telegramly(session, delay_ms):
    """telegramly.com — independent EN-leaning directory."""
    return await _crawl_directory(session, "telegramly", "https://telegramly.com/", delay_ms)


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
    return await _crawl_search_engine(session, "duckduckgo",
        "https://duckduckgo.com/",
        "https://duckduckgo.com/html/?q={q}",
        queries, delay_ms)

async def _stage2_yandex(session, queries, delay_ms):
    return await _crawl_search_engine(session, "yandex",
        "https://yandex.com/",
        "https://yandex.com/search/?text={q}",
        queries, delay_ms)

async def _stage2_brave(session, queries, delay_ms):
    return await _crawl_search_engine(session, "brave",
        "https://search.brave.com/",
        "https://search.brave.com/search?q={q}&source=web",
        queries, delay_ms)

async def _stage2_bing(session, queries, delay_ms):
    return await _crawl_search_engine(session, "bing",
        "https://www.bing.com/",
        "https://www.bing.com/search?q={q}",
        queries, delay_ms)

async def _stage2_mojeek(session, queries, delay_ms):
    return await _crawl_search_engine(session, "mojeek",
        "https://www.mojeek.com/",
        "https://www.mojeek.com/search?q={q}",
        queries, delay_ms)

async def _stage2_startpage(session, queries, delay_ms):
    return await _crawl_search_engine(session, "startpage",
        "https://www.startpage.com/",
        "https://www.startpage.com/do/search?q={q}",
        queries, delay_ms)

async def _stage2_google(session, queries, delay_ms):
    """Google web search with dork queries (site:t.me ...).
    Google is aggressive about CAPTCHA; the cool-down logic in stage2 will
    park this source for 6h after 3 consecutive zero/error responses."""
    # Use the simpler /search endpoint that more often serves HTML directly.
    return await _crawl_search_engine(session, "google",
        "https://www.google.com/",
        "https://www.google.com/search?q={q}&hl=en&num=30",
        queries, delay_ms, max_q=6)


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


# Map source name → (adapter, kind) where kind is 'kw' (uses query list) or 'plain'
_STAGE2_SOURCES = {
    # Telegram-specific directories
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
    "telegramly":   (_stage2_telegramly,   "plain"),
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
}


# Default source set when the user's `sources` field is blank — this drives
# fresh installs and acts as a fallback. Listed in the order we prefer.
_DEFAULT_SOURCES = ",".join([
    "internal",
    "tgstat", "telemetrio", "combot",
    "tdirectory", "tlgrm", "telegramic", "tgchannels", "searchtg",
    "tdoru", "telegaio", "telegramly",
    "google", "duckduckgo", "brave", "bing", "mojeek", "startpage", "yandex", "ecosia",
    "reddit", "hackernews", "github",
])


async def stage2_crawl_web(settings: dict) -> int:
    raw = (settings.get("sources") or "").strip()
    if not raw:
        # Empty config → run the full default set (all registered adapters
        # except 'internal', which is handled in stage 1)
        raw = _DEFAULT_SOURCES
    sources = [s.strip().lower() for s in raw.split(",") if s.strip() and s.strip().lower() != "internal"]
    delay_ms = int(settings.get("web_request_delay_ms") or 2500)
    keywords = _smart_keywords(settings.get("keywords") or "")
    queries = _build_search_queries(keywords)
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
                f"(of {len(_STAGE2_SOURCES)} registered)")
    if known_skipped:
        # Surface the skip so the user notices when their saved list is stale
        # against newly-registered adapters.
        _emit_event("stage2",
                    f"Skipped {len(known_skipped)} registered source(s): "
                    f"{', '.join(known_skipped)} — clear the Sources field "
                    f"in Hunter settings to enable them.",
                    "warn")
    if unknown:
        _emit_event("stage2",
                    f"Unknown source(s) in config: {', '.join(unknown)}",
                    "warn")

    # aiohttp session with cookies enabled (jar) — many sites set first-party
    # cookies on homepage that we need to echo back on subsequent fetches.
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar) as session:
        all_found: Dict[str, Set[str]] = {}
        for i, src in enumerate(valid_sources):
            interrupt = _check_interrupt("stage2")
            if interrupt == "cancel":
                _emit_event("stage2", "Cancelled by user", "warn"); break
            if interrupt == "skip":
                status["skip_stage_requested"] = False
                _emit_event("stage2", "Stage skipped by user", "warn"); break

            # Skip sources still in cool-down from previous runs/today
            if not _source_can_run(src):
                cd_ts = _SOURCE_COOLDOWN_UNTIL.get(src, 0)
                cd_str = datetime.fromtimestamp(cd_ts).isoformat(timespec="minutes")
                _emit_event("stage2", f"{src}: in cool-down until {cd_str}", "warn")
                status["stage_detail"]["sources_done"] = i + 1
                continue

            status["stage_detail"]["current_source"] = src
            status["stage_detail"]["sources_done"] = i
            status["stage_detail"]["per_source"][src] = {"state": "running", "found": 0}
            _emit_event("stage2", f"Source {src} starting…")

            adapter, kind = _STAGE2_SOURCES[src]
            try:
                if kind == "plain":
                    res = await adapter(session, delay_ms)
                elif kind == "kw":
                    res = await adapter(session, keywords, delay_ms)
                else:  # query
                    res = await adapter(session, queries, delay_ms)
                all_found[src] = res
                status["stage_detail"]["per_source"][src] = {"state": "done", "found": len(res)}
                logger.info(f"hunter[{src}]: {len(res)} candidates")
                _emit_event("stage2", f"{src}: {len(res)} candidates found")
                if len(res) > 0:
                    _source_record_success(src)
                else:
                    # Zero results often means we are being blocked silently
                    _source_record_failure(src)
            except Exception as e:
                status["stage_detail"]["per_source"][src] = {"state": "error", "found": 0, "error": str(e)[:60]}
                _emit_event("stage2", f"{src} failed: {str(e)[:80]}", "warn")
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
    return n_added


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


def _file_group(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    for g, exts in _FILE_GROUPS.items():
        if e in exts:
            return g
    return "other"


def _score_breakdown(file_count: int, sampled: int, members: int,
                      diversity: int, days_since_last: float) -> float:
    if sampled <= 0:
        return 0.0
    density = file_count / sampled       # 0..1
    member_score = min(1.0, (members or 0) / 50000.0)
    recency = max(0.0, 1.0 - (days_since_last / 30.0)) if days_since_last is not None else 0.0
    diversity_score = min(1.0, diversity / 5.0)
    # weighted
    return round(100 * (0.45 * density + 0.20 * member_score + 0.20 * recency + 0.15 * diversity_score), 2)


async def _enrich_one(client, candidate_id: int, username: str, sample_limit: int,
                       cand: Optional[dict] = None) -> bool:
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
            return False
        except ChannelPrivateError:
            # Inaccessible to this account — blacklist + delete (try a different
            # account by un-blacklisting it manually if you want to retry).
            await database.add_to_blacklist(username, "auto: private/inaccessible")
            await database.delete_hunter_candidate(candidate_id)
            return False
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s on get_entity({username})")
            raise
        except Exception as e:
            # Unknown errors (network, parse, etc.): blacklist + delete to avoid
            # it showing up forever. User can clean blacklist if needed.
            await database.add_to_blacklist(username, f"auto: {str(e)[:150]}")
            await database.delete_hunter_candidate(candidate_id)
            return False

    is_channel = isinstance(entity, Channel)
    title = getattr(entity, "title", None) or username

    # member count via GetFullChannelRequest
    members = None
    description = None
    try:
        full = await client(GetFullChannelRequest(entity))
        members = getattr(full.full_chat, "participants_count", None)
        description = getattr(full.full_chat, "about", None)
    except Exception:
        pass

    # sample recent messages (documents only, limit N)
    file_count = 0
    breakdown: Dict[str, int] = {k: 0 for k in list(_FILE_GROUPS.keys()) + ["other"]}
    last_message_at = None
    sampled = 0
    total_size = 0
    try:
        async for msg in client.iter_messages(entity, limit=sample_limit, filter=InputMessagesFilterDocument):
            sampled += 1
            if msg.document:
                file_count += 1
                total_size += int(getattr(msg.document, "size", 0) or 0)
                # extract extension from filename attribute
                fname = None
                for attr in (msg.document.attributes or []):
                    if isinstance(attr, DocumentAttributeFilename):
                        fname = attr.file_name
                        break
                ext = (fname.rsplit(".", 1)[-1] if fname and "." in fname else "")
                breakdown[_file_group(ext)] += 1
            if msg.date and (last_message_at is None or msg.date > last_message_at):
                last_message_at = msg.date
    except FloodWaitError:
        raise
    except Exception as e:
        logger.debug(f"message sample failed for {username}: {e}")

    avg_size = int(total_size / file_count) if file_count else 0
    diversity = sum(1 for v in breakdown.values() if v > 0)
    days_since = None
    if last_message_at:
        if last_message_at.tzinfo is None:
            last_message_at = last_message_at.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last_message_at).total_seconds() / 86400
    score = _score_breakdown(file_count, sampled or sample_limit, members or 0, diversity, days_since or 999)

    # Cache peer_id + access_hash so future API calls (deep_scan, join, …)
    # can build InputPeerChannel directly and skip ResolveUsernameRequest,
    # which has a very strict per-account daily limit.
    pid = getattr(entity, "id", None)
    ahash = getattr(entity, "access_hash", None)

    await database.update_hunter_candidate(candidate_id, {
        "title": title,
        "description": (description or "")[:500],
        "is_channel": is_channel,
        "members": members,
        "sampled_messages": sampled,
        "file_count_sample": file_count,
        "estimated_files": file_count,   # rough; full backfill would need pagination
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
    return True


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
        _emit_event("stage3", "Telegram hesabı yetkisiz; stage 3 atlanıyor", "warn")
        return 0, 0

    rows, _ = await database.list_hunter_candidates(status="discovered", limit=budget, offset=0, sort="discovered_at")
    enriched, failed = 0, 0
    sample_limit = int(settings.get("tg_messages_to_sample") or 200)
    cached_only_mode = False
    skipped_no_cache = 0

    if not rows:
        _emit_event("stage3", "Zenginleştirilecek aday yok — hepsi zaten enriched/joined/rejected", "info")
        return 0, 0

    # Sort to put cached-peer candidates FIRST so we make progress even if
    # we eventually hit ResolveUsername limits.
    rows = sorted(rows, key=lambda r: 0 if (r.get("peer_id") and r.get("access_hash") is not None) else 1)
    cached_count = sum(1 for r in rows if r.get("peer_id") and r.get("access_hash") is not None)
    _emit_event("stage3", f"{len(rows)} aday zenginleştirilecek ({cached_count} cache\'li, {len(rows)-cached_count} resolve gerekli)")
    status["total"] = len(rows)
    status["progress"] = 0

    for r in rows:
        interrupt = _check_interrupt("stage3")
        if interrupt == "cancel":
            _emit_event("stage3", "Cancelled by user", "warn")
            break
        if interrupt == "skip":
            status["skip_stage_requested"] = False
            _emit_event("stage3", "Stage skipped by user", "warn")
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
            ok = await _enrich_one(client, r["id"], username, sample_limit, cand=dict(r))
            if ok:
                enriched += 1
                _emit_event("stage3", f"@{username}: enriched ✓")
            else:
                failed += 1
                _emit_event("stage3", f"@{username}: failed", "warn")
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
                        f"Telegram username çözümleme limiti dolu (FloodWait {hrs}s {mins}d). "
                        f"Cache'siz adaylar atlanacak; cache'li adaylar işlenmeye devam ediyor."
                    )
                    logger.warning(
                        f"Hunter: ResolveUsername limit hit ({backoff}s) — switching to cached-only"
                    )
                    _emit_event("stage3", msg, "warn")
                    status["error"] = None  # not a fatal error anymore
                # Skip THIS candidate (it had no cache and triggered the limit)
                # but continue processing the rest of the queue
                skipped_no_cache += 1
                continue
            logger.warning(f"Hunter FloodWait — sleeping {backoff}s")
            _emit_event("stage3", f"FloodWait {backoff}s, bekleniyor…", "warn")
            await _interruptible_sleep(max(60, backoff))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failed += 1
            logger.warning(f"Enrich error for {username}: {e}")
        await asyncio.sleep(delay_ms / 1000)

    if skipped_no_cache > 0:
        _emit_event("stage3", f"{skipped_no_cache} aday cache yokluğundan atlandı; ResolveUsername limiti yenilenince işlenecek", "warn")
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
            "stage_detail": {}, "events": [],
            "cancel_requested": False, "skip_stage_requested": False,
            "stage_started_at": None,
        })
        _emit_event("run", "Hunter run started")

    settings = await database.get_hunter_settings()
    run_id = await database.start_hunter_run(note="manual")
    seeds_found = enriched = failed = 0
    try:
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 1", "warn"); raise asyncio.CancelledError
        if settings.get("stage1_enabled"):
            status["stage"] = "stage1"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            status["stage_detail"] = {}
            _emit_event("stage1", "Stage 1: scanning internal links & mentions")
            seeds_found += await stage1_mine_internal()
            status["seeds_found"] = seeds_found
            _emit_event("stage1", f"Stage 1 done: {seeds_found} seeds total")
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 2", "warn"); raise asyncio.CancelledError
        # skip flag at stage boundary: clear and proceed to next stage
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        if settings.get("stage2_enabled"):
            status["stage"] = "stage2"
            status["stage_started_at"] = datetime.utcnow().isoformat()
            seeds_found += await stage2_crawl_web(settings)
            status["seeds_found"] = seeds_found
            _emit_event("stage2", f"Stage 2 done: {seeds_found} seeds total")
        if status.get("cancel_requested"):
            _emit_event("run", "Cancelled before stage 3", "warn"); raise asyncio.CancelledError
        if status.get("skip_stage_requested"):
            status["skip_stage_requested"] = False
        status["stage"] = "stage3"
        status["stage_started_at"] = datetime.utcnow().isoformat()
        status["stage_detail"] = {}
        _emit_event("stage3", "Stage 3: enriching candidates via Telegram")
        e, f = await stage3_enrich_pending(settings)
        enriched, failed = e, f
        status["enriched"] = enriched
        status["failed"] = failed
        _emit_event("stage3", f"Stage 3 done: {enriched} enriched, {failed} failed")
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
    _emit_event("run", "Cancel requested by user", "warn")
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
    _emit_event(status.get("stage") or "run", "Skip stage requested by user", "warn")
    return True


def kick_run() -> bool:
    """Start a hunter run in the background. Returns False if already running."""
    global _run_task
    if status["running"]:
        return False
    _run_task = asyncio.create_task(run_hunter_once())
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
        return {"ok": True, "group_id": gid}
    except FloodWaitError as e:
        # Persist for the retry worker to pick up later. Return ok=True so
        # the frontend treats this as a "queued" action rather than a hard
        # failure — a different toast surfaces the wait info.
        await database.enqueue_join(
            candidate_id, account_id, int(e.seconds),
            last_error=f"flood wait {e.seconds}s",
        )
        return {
            "ok": True,
            "queued": True,
            "wait_s": int(e.seconds),
            "candidate_id": candidate_id,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


# ── Deep scan: pull EVERY document message from a candidate ──────────────────

deep_scan_status: Dict[int, dict] = {}        # {candidate_id: {state, processed, total, error}}
_deep_scan_tasks: Dict[int, asyncio.Task] = {}


def _file_group_for_ext(ext: str) -> str:
    return _file_group(ext)


async def _scan_iter_documents(client, entity, candidate_id: int,
                                 username: str, delay_ms: float,
                                 starting_n: int = 0):
    """Walk all document messages of an entity into hunter_candidate_files,
    resuming from offset_id on FloodWait. Returns (n, breakdown, total_size, last_at)."""
    n = starting_n
    breakdown = {k: 0 for k in list(_FILE_GROUPS.keys()) + ["other"]}
    total_size = 0
    last_at = None
    offset_id = 0
    consecutive_floodwaits = 0
    while True:
        try:
            async for msg in client.iter_messages(entity, offset_id=offset_id,
                                                   filter=InputMessagesFilterDocument):
                if not msg.document:
                    offset_id = msg.id
                    continue
                n += 1
                offset_id = msg.id
                size = int(getattr(msg.document, "size", 0) or 0)
                total_size += size
                fname = None
                for attr in (msg.document.attributes or []):
                    if isinstance(attr, DocumentAttributeFilename):
                        fname = attr.file_name
                        break
                ext = (fname.rsplit(".", 1)[-1] if fname and "." in fname else "")
                grp = _file_group(ext)
                breakdown[grp] += 1
                date = msg.date
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date and (last_at is None or date > last_at):
                    last_at = date
                await database.insert_candidate_file(
                    candidate_id, msg.id, fname, ext.lower(), size, grp, date,
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
    return n, breakdown, total_size, last_at


async def deep_scan_candidate(candidate_id: int):
    cand = await database.get_hunter_candidate(candidate_id)
    if not cand:
        return
    username = cand["username"]
    settings = await database.get_hunter_settings()
    account_id = int(settings.get("tg_account_id") or 1)
    temp_join_enabled = bool(settings.get("tg_temp_join_enabled"))

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
        n, breakdown, total_size, last_at = await _scan_iter_documents(
            client, entity, candidate_id, username, delay_ms,
        )

        temp_joined = False
        # Heuristic: if we got 0 documents but the channel reports many members,
        # it's likely a private/restricted-history channel. With user opt-in,
        # try a temporary join → re-scan → leave.
        members_hint = cand.get("members") or 0
        if n == 0 and temp_join_enabled and members_hint > 50:
            logger.info(
                f"Hunter: 0 docs from @{username} but {members_hint} members — temp-joining for scan"
            )
            try:
                await client(JoinChannelRequest(entity))
                temp_joined = True
                deep_scan_status[candidate_id] = {
                    "state": "running", "processed": 0, "total": 0,
                    "error": "joined temporarily; rescanning…",
                }
                # Re-scan after joining
                n, breakdown, total_size, last_at = await _scan_iter_documents(
                    client, entity, candidate_id, username, delay_ms,
                )
            except FloodWaitError:
                # Couldn't join right now; surface and stop temp-join attempt.
                pass
            except Exception as e:
                logger.warning(f"Temp-join failed for @{username}: {e}")
            finally:
                # Always leave if we joined — user makes the real "join" call.
                if temp_joined:
                    try:
                        await client(LeaveChannelRequest(entity))
                        logger.info(f"Hunter: left @{username} after temp scan")
                    except Exception as e:
                        logger.warning(f"Hunter: leave-after-tempjoin failed for @{username}: {e}")

        # Finalize: update candidate aggregate stats from full data
        avg_size = int(total_size / n) if n else 0
        diversity = sum(1 for v in breakdown.values() if v > 0)
        days_since = None
        if last_at:
            days_since = (datetime.now(timezone.utc) - last_at).total_seconds() / 86400
        # Re-score using full data (more reliable than 200-msg sample)
        # Using same formula but density now is 1.0 (we only counted files)
        member_score = min(1.0, (cand.get("members") or 0) / 50000.0)
        recency = max(0.0, 1.0 - (days_since / 30.0)) if days_since is not None else 0.0
        diversity_score = min(1.0, diversity / 5.0)
        score = round(100 * (0.45 * 1.0 + 0.20 * member_score + 0.20 * recency + 0.15 * diversity_score), 2)

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
        deep_scan_status[candidate_id] = {"state": "done", "processed": n, "total": n, "error": None}
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
