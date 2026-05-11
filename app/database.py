import os
import asyncpg
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: Optional[asyncpg.Pool] = None

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS groups (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT,
    is_channel BOOLEAN DEFAULT FALSE,
    last_synced_message_id BIGINT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    display_name TEXT,
    excluded BOOLEAN DEFAULT FALSE,
    hidden BOOLEAN DEFAULT FALSE,
    last_link_message_id BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    message_id BIGINT NOT NULL,
    file_name TEXT,
    file_ext TEXT,
    mime_type TEXT,
    file_size BIGINT DEFAULT 0,
    date TIMESTAMPTZ NOT NULL,
    local_path TEXT,
    downloaded_at TIMESTAMPTZ,
    downloading BOOLEAN DEFAULT FALSE,
    download_progress REAL DEFAULT 0,
    context TEXT,
    UNIQUE(group_id, message_id)
);

CREATE TABLE IF NOT EXISTS links (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    message_id BIGINT NOT NULL,
    platform TEXT,
    url TEXT NOT NULL,
    context TEXT,
    date TIMESTAMPTZ NOT NULL,
    -- Result of the link prober: NULL = not yet visited, TRUE = file(s)
    -- found, FALSE = link is dead / inaccessible / 404 etc.
    probed_at TIMESTAMPTZ,
    available BOOLEAN,
    file_count INTEGER,
    file_size_total BIGINT,
    files_json JSONB,
    probe_error TEXT,
    UNIQUE(group_id, message_id, url)
);
ALTER TABLE links ADD COLUMN IF NOT EXISTS probed_at TIMESTAMPTZ;
ALTER TABLE links ADD COLUMN IF NOT EXISTS available BOOLEAN;
ALTER TABLE links ADD COLUMN IF NOT EXISTS file_count INTEGER;
ALTER TABLE links ADD COLUMN IF NOT EXISTS file_size_total BIGINT;
ALTER TABLE links ADD COLUMN IF NOT EXISTS files_json JSONB;
ALTER TABLE links ADD COLUMN IF NOT EXISTS probe_error TEXT;
CREATE INDEX IF NOT EXISTS idx_links_probed ON links (probed_at NULLS FIRST);
CREATE INDEX IF NOT EXISTS idx_links_avail  ON links (available);

CREATE INDEX IF NOT EXISTS idx_files_name_trgm ON files USING GIN (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_files_ext       ON files (file_ext);
CREATE INDEX IF NOT EXISTS idx_files_date      ON files (date DESC);
CREATE INDEX IF NOT EXISTS idx_files_group     ON files (group_id);
CREATE INDEX IF NOT EXISTS idx_files_size      ON files (file_size DESC);
CREATE INDEX IF NOT EXISTS idx_files_grp_date  ON files (group_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_files_name_size ON files (file_name, file_size);
CREATE INDEX IF NOT EXISTS idx_files_ext_date  ON files (file_ext, date DESC);
CREATE INDEX IF NOT EXISTS idx_links_plat_date ON links (platform, date DESC, group_id);
CREATE INDEX IF NOT EXISTS idx_links_grp_date  ON links (group_id, date DESC);

CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    api_id BIGINT NOT NULL,
    api_hash TEXT NOT NULL,
    phone TEXT,
    display_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS account_groups (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    last_synced_message_id BIGINT DEFAULT 0,
    last_link_message_id BIGINT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    display_name TEXT,
    excluded BOOLEAN DEFAULT FALSE,
    hidden BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (account_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_acc_groups_acc ON account_groups (account_id);
CREATE INDEX IF NOT EXISTS idx_acc_groups_grp ON account_groups (group_id);

CREATE TABLE IF NOT EXISTS telemetry_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT TRUE,
    install_id TEXT,
    endpoint_url TEXT DEFAULT 'https://telfiles-telemetry.example.com/telemetry.php',
    interval_seconds INTEGER DEFAULT 86400,
    last_sent_at TIMESTAMPTZ,
    last_sent_status INTEGER,
    last_sent_error TEXT,
    next_send_at TIMESTAMPTZ
);

ALTER TABLE groups ADD COLUMN IF NOT EXISTS member_count BIGINT;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS member_count_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS hunter_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT FALSE,
    -- Stage 1 (internal mining)
    stage1_enabled BOOLEAN DEFAULT TRUE,
    -- Stage 2 (web crawl)
    stage2_enabled BOOLEAN DEFAULT TRUE,
    web_request_delay_ms INTEGER DEFAULT 2500,
    web_concurrency INTEGER DEFAULT 2,
    -- Stage 3 (Telethon enrichment)
    tg_concurrency INTEGER DEFAULT 1,
    tg_request_delay_ms INTEGER DEFAULT 1500,
    tg_daily_lookup_cap INTEGER DEFAULT 500,
    tg_messages_to_sample INTEGER DEFAULT 200,
    tg_account_id INTEGER DEFAULT 1,
    -- Schedule
    schedule_kind TEXT DEFAULT 'manual', -- 'manual' | 'interval'
    schedule_interval_seconds INTEGER DEFAULT 86400,
    -- Filters
    keywords TEXT,                 -- comma separated; used for web/tgsearch queries
    min_subscribers INTEGER DEFAULT 0,
    languages TEXT,                -- comma separated language codes ('tr','en'…) or empty
    sources TEXT DEFAULT '',  -- empty = run every adapter registered in hunter._STAGE2_SOURCES
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS hunter_candidates (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,           -- normalized: '@xxx' lowercased without @
    title TEXT,
    description TEXT,
    is_channel BOOLEAN DEFAULT TRUE,
    members INTEGER,
    language TEXT,
    sampled_messages INTEGER DEFAULT 0,
    file_count_sample INTEGER DEFAULT 0,
    estimated_files INTEGER,
    avg_file_size BIGINT,
    last_message_at TIMESTAMPTZ,
    file_type_breakdown JSONB,                -- {audio:N,video:N,image:N,archive:N,document:N,software:N,other:N}
    score REAL DEFAULT 0,
    status TEXT DEFAULT 'discovered',          -- discovered | enriched | reviewed | joined | rejected | blacklisted | failed
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    enriched_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_hunter_status ON hunter_candidates (status);
CREATE INDEX IF NOT EXISTS idx_hunter_score  ON hunter_candidates (score DESC);
CREATE INDEX IF NOT EXISTS idx_hunter_disc   ON hunter_candidates (discovered_at DESC);

CREATE TABLE IF NOT EXISTS hunter_sources (
    id BIGSERIAL PRIMARY KEY,
    candidate_id BIGINT NOT NULL REFERENCES hunter_candidates(id) ON DELETE CASCADE,
    source TEXT NOT NULL,                      -- 'internal:mention' | 'internal:link' | 'tgstat' | 'bing' | …
    detail TEXT,                               -- e.g. group_id where mention was found, or web URL
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(candidate_id, source, detail)
);

CREATE INDEX IF NOT EXISTS idx_hunter_src_cand ON hunter_sources (candidate_id);

CREATE TABLE IF NOT EXISTS hunter_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    seeds_found INTEGER DEFAULT 0,
    enriched INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    error TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS hunter_blacklist (
    username TEXT PRIMARY KEY,
    reason TEXT,
    added_at TIMESTAMPTZ DEFAULT NOW()
);

-- FloodWait retry queue. join_candidate enqueues here when Telegram returns
-- a FloodWaitError; a background worker picks up due entries and re-attempts.
CREATE TABLE IF NOT EXISTS hunter_join_queue (
    candidate_id BIGINT PRIMARY KEY REFERENCES hunter_candidates(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER DEFAULT 1,
    last_error TEXT,
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_join_queue_due ON hunter_join_queue (due_at);

CREATE TABLE IF NOT EXISTS hunter_candidate_files (
    candidate_id BIGINT NOT NULL REFERENCES hunter_candidates(id) ON DELETE CASCADE,
    message_id BIGINT NOT NULL,
    file_name TEXT,
    file_ext TEXT,
    file_size BIGINT DEFAULT 0,
    file_group TEXT,
    date TIMESTAMPTZ,
    PRIMARY KEY (candidate_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_hunter_files_cand ON hunter_candidate_files (candidate_id);
CREATE INDEX IF NOT EXISTS idx_hunter_files_date ON hunter_candidate_files (date DESC);
CREATE INDEX IF NOT EXISTS idx_hunter_files_ext  ON hunter_candidate_files (file_ext);

ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_status TEXT;          -- NULL | 'queued' | 'running' | 'done' | 'error'
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_progress INTEGER DEFAULT 0;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_total INTEGER DEFAULT 0;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_at TIMESTAMPTZ;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_error TEXT;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS tg_temp_join_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS peer_id BIGINT;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS access_hash BIGINT;

CREATE TABLE IF NOT EXISTS watch_terms (
    id SERIAL PRIMARY KEY,
    keywords TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    baseline_file_id BIGINT NOT NULL DEFAULT 0,
    last_checked_file_id BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_notifications (
    id SERIAL PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watch_terms(id) ON DELETE CASCADE,
    file_ids BIGINT[] NOT NULL DEFAULT '{}',
    first_match_at TIMESTAMPTZ DEFAULT NOW(),
    last_match_at TIMESTAMPTZ DEFAULT NOW(),
    dismissed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notif_watch_active ON watch_notifications (watch_id) WHERE dismissed_at IS NULL;
"""


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    await _migrate_to_multi_account()


async def _migrate_to_multi_account():
    # Add discovered_by_account_id columns if missing (idempotent)
    await _exec("""ALTER TABLE files ADD COLUMN IF NOT EXISTS discovered_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL""")
    await _exec("""ALTER TABLE links ADD COLUMN IF NOT EXISTS discovered_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL""")

    # If no accounts exist but legacy credentials/session is present, seed default Account 1
    n = await _qval("SELECT COUNT(*) FROM accounts")
    if (n or 0) == 0:
        import os, json
        creds_path = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "credentials.json")
        api_id = None; api_hash = None
        try:
            if os.path.exists(creds_path):
                with open(creds_path) as f:
                    d = json.load(f)
                api_id = int(d.get("api_id") or 0) or None
                api_hash = (d.get("api_hash") or "") or None
        except Exception:
            pass
        if not api_id:
            # Tolerate TELEGRAM_API_ID being unset OR set to "" (install.sh
            # writes an empty value when the user skips the prompt). int("")
            # would raise ValueError and crash the lifespan handler — leaving
            # the container in a restart loop with no accounts seeded.
            _raw_id = (os.environ.get("TELEGRAM_API_ID") or "").strip()
            try:
                api_id = int(_raw_id) or None if _raw_id else None
            except ValueError:
                api_id = None
            api_hash = (os.environ.get("TELEGRAM_API_HASH") or "").strip() or None
        if api_id and api_hash:
            await _exec(
                "INSERT INTO accounts (id, name, api_id, api_hash) VALUES ($1, $2, $3, $4)",
                1, "Hesap 1", api_id, api_hash,
            )
            # Reset SERIAL so next account gets id 2
            await _exec("SELECT setval(pg_get_serial_sequence('accounts','id'), 1)")

    # Backfill account_groups for default account from legacy groups columns
    has_default = await _qval("SELECT 1 FROM accounts WHERE id = 1")
    if has_default:
        # Insert any missing (account=1, group) rows from groups into account_groups
        await _exec("""
            INSERT INTO account_groups (account_id, group_id, last_synced_message_id,
                                         last_link_message_id, last_synced_at,
                                         display_name, excluded, hidden)
            SELECT 1, g.id,
                   COALESCE(g.last_synced_message_id, 0),
                   COALESCE(g.last_link_message_id, 0),
                   g.last_synced_at,
                   g.display_name,
                   COALESCE(g.excluded, FALSE),
                   COALESCE(g.hidden, FALSE)
            FROM groups g
            ON CONFLICT (account_id, group_id) DO NOTHING
        """)
        # Set discovered_by_account_id=1 for legacy rows (NULL)
        await _exec("UPDATE files SET discovered_by_account_id = 1 WHERE discovered_by_account_id IS NULL")
        await _exec("UPDATE links SET discovered_by_account_id = 1 WHERE discovered_by_account_id IS NULL")


async def _q(sql: str, *args):
    async with _pool.acquire() as conn:
        return await conn.fetch(sql, *args)

async def _qrow(sql: str, *args):
    async with _pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)

async def _qval(sql: str, *args):
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *args)

async def _exec(sql: str, *args):
    async with _pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def upsert_group(group_id: int, name: str, username: Optional[str], is_channel: bool):
    await _exec(
        """INSERT INTO groups (id, name, username, is_channel)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (id) DO UPDATE SET
               name = EXCLUDED.name,
               username = EXCLUDED.username,
               is_channel = EXCLUDED.is_channel""",
        group_id, name, username, is_channel,
    )


async def set_group_settings(
    group_id: int,
    display_name: Optional[str] = None,
    excluded: Optional[int] = None,
    hidden: Optional[int] = None,
):
    parts = []
    args: List[Any] = []
    idx = 1
    if display_name is not None:
        parts.append(f"display_name = ${idx}"); args.append(display_name); idx += 1
    if excluded is not None:
        parts.append(f"excluded = ${idx}"); args.append(bool(excluded)); idx += 1
    if hidden is not None:
        parts.append(f"hidden = ${idx}"); args.append(bool(hidden)); idx += 1
    if not parts:
        return
    args.append(group_id)
    await _exec(f"UPDATE groups SET {', '.join(parts)} WHERE id = ${idx}", *args)


async def get_excluded_group_ids() -> List[int]:
    rows = await _q("SELECT id FROM groups WHERE excluded = TRUE")
    return [r['id'] for r in rows]


async def is_group_excluded(group_id: int) -> bool:
    v = await _qval("SELECT excluded FROM groups WHERE id = $1", group_id)
    return bool(v)


async def get_group_by_id(group_id: int) -> Optional[Dict]:
    row = await _qrow(
        """SELECT id, name, username, is_channel, excluded, hidden,
                  COALESCE(display_name, name) AS display_name
           FROM groups WHERE id = $1""",
        group_id,
    )
    return dict(row) if row else None


async def delete_group_data(group_id: int):
    """Remove the group's files, links and the group row itself."""
    await _exec("DELETE FROM files WHERE group_id = $1", group_id)
    await _exec("DELETE FROM links WHERE group_id = $1", group_id)
    await _exec("DELETE FROM groups WHERE id = $1", group_id)


async def reset_group_watermark(group_id: int):
    """Zero the sync watermark so the next sync re-walks the whole history.
    Existing files/links are kept; insert_file's UniqueViolation handles dedup."""
    await _exec(
        """UPDATE groups
           SET last_synced_message_id = 0,
               last_link_message_id   = 0
           WHERE id = $1""",
        group_id,
    )


async def get_groups() -> List[Dict]:
    rows = await _q(
        """SELECT g.id, g.name, g.username, g.is_channel,
                  g.excluded, g.hidden,
                  COALESCE(g.display_name, g.name) AS display_name,
                  g.last_synced_at,
                  COUNT(f.id)                          AS file_count,
                  COALESCE(SUM(f.file_size), 0)        AS total_size
           FROM groups g
           LEFT JOIN files f ON f.group_id = g.id
           GROUP BY g.id
           ORDER BY g.name"""
    )
    return [dict(r) for r in rows]


async def get_last_synced_message_id(group_id: int) -> int:
    v = await _qval("SELECT last_synced_message_id FROM groups WHERE id = $1", group_id)
    return v or 0


async def update_last_synced(group_id: int, message_id: int):
    await _exec(
        "UPDATE groups SET last_synced_message_id = $1, last_synced_at = $2 WHERE id = $3",
        message_id, datetime.utcnow(), group_id,
    )


async def get_last_synced_link_id(group_id: int) -> int:
    v = await _qval("SELECT last_link_message_id FROM groups WHERE id = $1", group_id)
    return v or 0


async def update_last_synced_links(group_id: int, message_id: int):
    await _exec(
        "UPDATE groups SET last_link_message_id = $1 WHERE id = $2",
        message_id, group_id,
    )


async def insert_file(
    group_id: int,
    message_id: int,
    file_name: Optional[str],
    file_ext: Optional[str],
    mime_type: Optional[str],
    file_size: int,
    date: str,
    context: Optional[str] = None,
    discovered_by_account_id: Optional[int] = None,
) -> bool:
    try:
        date_ts = datetime.fromisoformat(date.replace("Z", "+00:00")) if date else datetime.utcnow()
        await _exec(
            """INSERT INTO files
               (group_id, message_id, file_name, file_ext, mime_type, file_size, date, context, discovered_by_account_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            group_id, message_id, file_name, file_ext, mime_type, file_size, date_ts, context, discovered_by_account_id,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def insert_link(
    group_id: int,
    message_id: int,
    platform: Optional[str],
    url: str,
    context: Optional[str],
    date: str,
    discovered_by_account_id: Optional[int] = None,
) -> bool:
    try:
        date_ts = datetime.fromisoformat(date.replace("Z", "+00:00")) if date else datetime.utcnow()
        await _exec(
            """INSERT INTO links (group_id, message_id, platform, url, context, date, discovered_by_account_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            group_id, message_id, platform, url, context, date_ts, discovered_by_account_id,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


# ── Link probe helpers ────────────────────────────────────────────────────────
import json as _json

async def get_links_due_for_probe(limit: int = 50, stale_days: int = 7) -> List[Dict]:
    """Pick up unprobed links first, then ones stale enough to recheck."""
    rows = await _q(
        """SELECT id, url, platform
             FROM links
            WHERE probed_at IS NULL
               OR probed_at < NOW() - INTERVAL '1 day' * $1
            ORDER BY probed_at NULLS FIRST, id DESC
            LIMIT $2""",
        stale_days, limit,
    )
    return [dict(r) for r in rows]


async def record_probe_result(
    link_id: int,
    available: Optional[bool],
    files: List[Dict],
    error: Optional[str],
):
    file_count = len(files) if files else 0
    file_size_total = sum(int(f.get("size") or 0) for f in (files or []))
    files_payload = _json.dumps(files or [])
    await _exec(
        """UPDATE links
              SET probed_at = NOW(),
                  available = $2,
                  file_count = $3,
                  file_size_total = $4,
                  files_json = $5::jsonb,
                  probe_error = $6
            WHERE id = $1""",
        link_id, available, file_count, file_size_total, files_payload, error,
    )


_SORT_COLS = {"date": "f.date", "name": "f.file_name", "size": "f.file_size", "group": "g.name"}
_SORT_DIRS = {"asc": "ASC", "desc": "DESC"}

_EXT_GROUPS: Dict[str, List[str]] = {
    "audio":    ["mp3","flac","wav","aac","ogg","m4a","opus","wma","ape","alac","mid","midi"],
    "video":    ["mp4","mkv","avi","mov","wmv","flv","webm","m4v","ts","vob","rm","rmvb","3gp"],
    "image":    ["jpg","jpeg","png","gif","bmp","webp","svg","tiff","tif","heic","ico","raw"],
    "archive":  ["zip","rar","7z","tar","gz","bz2","xz","zst","cab","ace","lzh","lz4","iso"],
    "document": ["pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","txt","epub","rtf","csv","md"],
    "software": ["exe","apk","dmg","deb","rpm","msi","pkg","bin","jar","sh","bat","ps1"],
}


async def search_files(
    query: str = "",
    ext: str = "",
    ext_group: str = "",
    group_id: Optional[int] = None,
    group_ids: Optional[List[int]] = None,
    file_ids: Optional[List[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    size_min: Optional[int] = None,
    size_max: Optional[int] = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    dedupe: bool = True,
) -> Tuple[List[Dict], int]:
    col       = _SORT_COLS.get(sort_by, "f.date")
    direction = _SORT_DIRS.get(sort_dir, "DESC")

    conditions: List[str] = []
    args: List[Any] = []
    idx = 1

    if query:
        conditions.append(f"f.file_name ILIKE ${idx}")
        args.append(f"%{query}%"); idx += 1
    if ext:
        conditions.append(f"LOWER(f.file_ext) = LOWER(${idx})")
        args.append(ext.lstrip(".")); idx += 1
    elif ext_group:
        exts = _EXT_GROUPS.get(ext_group, [])
        if exts:
            conditions.append(f"LOWER(f.file_ext) = ANY(${idx}::text[])")
            args.append(exts); idx += 1
    if file_ids:
        conditions.append(f"f.id = ANY(${idx}::bigint[])"); args.append(file_ids); idx += 1
    if group_ids:
        conditions.append(f"f.group_id = ANY(${idx}::bigint[])"); args.append(group_ids); idx += 1
    elif group_id is not None:
        conditions.append(f"f.group_id = ${idx}"); args.append(group_id); idx += 1
    if date_from:
        conditions.append(f"f.date >= ${idx}"); args.append(date_from); idx += 1
    if date_to:
        conditions.append(f"f.date <= ${idx}"); args.append(date_to + "T23:59:59"); idx += 1
    if size_min is not None:
        conditions.append(f"f.file_size >= ${idx}"); args.append(size_min); idx += 1
    if size_max is not None:
        conditions.append(f"f.file_size <= ${idx}"); args.append(size_max); idx += 1

    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    c_args = list(args)

    if not dedupe:
        # Raw view — every (group_id, message_id) row is its own line.
        args += [limit, offset]
        rows = await _q(
            f"""SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
                       f.mime_type, f.file_size, f.date, f.local_path,
                       f.downloading, f.download_progress, f.context,
                       1 AS appearances,
                       COALESCE(g.display_name, g.name) AS group_name,
                       g.username AS group_username
                FROM files f
                JOIN groups g ON g.id = f.group_id
                {where}
                ORDER BY {col} {direction}
                LIMIT ${idx} OFFSET ${idx+1}""",
            *args,
        )
        total = await _qval(
            f"SELECT COUNT(*) FROM files f JOIN groups g ON g.id = f.group_id {where}",
            *c_args,
        )
        return [dict(r) for r in rows], total or 0

    # Dedupe by (file_name, file_size) using a single window-function pass:
    #  - COUNT() OVER tells us how many rows share that (name,size) pair
    #  - ROW_NUMBER() OVER picks the canonical row, preferring a downloaded
    #    copy so triggerDownload/blob still works, otherwise the newest msg.
    # One scan beats the two-CTE approach (which became O(N²) in practice
    # because the IS NOT DISTINCT FROM join couldn't use an index).
    # Outer ORDER BY runs against the un-prefixed projection.
    outer_col = col.replace("f.", "").replace("g.name", "group_name")
    args += [limit, offset]

    cache_key = _dedupe_rows_key(where, args, outer_col, direction)
    cached_rows = _dedupe_rows_get(cache_key)
    if cached_rows is not None:
        total = await _files_dedupe_count_cached(where, c_args)
        return cached_rows, total or 0

    sql = f"""WITH ranked AS (
                SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
                       f.mime_type, f.file_size, f.date, f.local_path,
                       f.downloading, f.download_progress, f.context,
                       COALESCE(g.display_name, g.name) AS group_name,
                       g.username AS group_username,
                       COUNT(*) OVER (PARTITION BY f.file_name, f.file_size)::int AS appearances,
                       ROW_NUMBER() OVER (
                         PARTITION BY f.file_name, f.file_size
                         ORDER BY (f.local_path IS NULL), f.date DESC
                       ) AS _rn
                FROM files f
                JOIN groups g ON g.id = f.group_id
                {where}
            )
            SELECT id, group_id, message_id, file_name, file_ext, mime_type,
                   file_size, date, local_path, downloading, download_progress,
                   context, group_name, group_username, appearances
            FROM ranked
            WHERE _rn = 1
            ORDER BY {outer_col} {direction}
            LIMIT ${idx} OFFSET ${idx+1}"""

    # Force the parallel plan + give workers enough work_mem to keep the
    # sort in-RAM. Postgres' default cost model picks a slower serial plan
    # for this query because it overestimates parallel setup cost. Tested:
    # ~2.0s default → ~1.3s with these locals. SET LOCAL only affects this
    # transaction so it doesn't leak to other queries.
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL min_parallel_table_scan_size = '0'")
            await conn.execute("SET LOCAL parallel_setup_cost = 0")
            await conn.execute("SET LOCAL parallel_tuple_cost = 0")
            await conn.execute("SET LOCAL work_mem = '64MB'")
            rows = await conn.fetch(sql, *args)

    result = [dict(r) for r in rows]
    _dedupe_rows_set(cache_key, result)
    total = await _files_dedupe_count_cached(where, c_args)
    return result, total or 0


# Tiny TTL cache for the dedupe COUNT — slow query, slow-changing answer.
# (~752k row table; recomputing on every paginate/sort is wasteful.)
_DEDUPE_COUNT_CACHE: Dict[str, Tuple[float, int]] = {}
_DEDUPE_COUNT_TTL = 60.0

# Short-lived cache for the dedupe ROW results too. Repeat tab switches
# within the TTL hit instantly. We invalidate explicitly when files change
# (see invalidate_files_caches below), so 30s is just a safety upper bound.
_DEDUPE_ROWS_CACHE: Dict[str, Tuple[float, List[Dict]]] = {}
_DEDUPE_ROWS_TTL = 30.0


def _dedupe_rows_key(where: str, args: List, outer_col: str, direction: str) -> str:
    import hashlib, json as _j
    payload = where + "|" + outer_col + "|" + direction + "|" + _j.dumps(
        [str(a) for a in args], default=str
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _dedupe_rows_get(key: str):
    import time as _t
    cached = _DEDUPE_ROWS_CACHE.get(key)
    if cached and _t.time() - cached[0] < _DEDUPE_ROWS_TTL:
        return cached[1]
    return None


def _dedupe_rows_set(key: str, rows: List[Dict]) -> None:
    import time as _t
    _DEDUPE_ROWS_CACHE[key] = (_t.time(), rows)
    if len(_DEDUPE_ROWS_CACHE) > 64:
        oldest = min(_DEDUPE_ROWS_CACHE, key=lambda k: _DEDUPE_ROWS_CACHE[k][0])
        _DEDUPE_ROWS_CACHE.pop(oldest, None)


def invalidate_files_caches() -> None:
    """Drop both rows + count caches. Call after bulk insert/delete or
    when local_path / downloading state changes for many rows."""
    _DEDUPE_ROWS_CACHE.clear()
    _DEDUPE_COUNT_CACHE.clear()


def _dedupe_count_key(where: str, c_args: List) -> str:
    import hashlib, json as _j
    payload = where + "|" + _j.dumps([str(a) for a in c_args], default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _files_dedupe_count_cached(where: str, c_args: List) -> int:
    import time as _t
    key = _dedupe_count_key(where, c_args)
    now = _t.time()
    cached = _DEDUPE_COUNT_CACHE.get(key)
    if cached and now - cached[0] < _DEDUPE_COUNT_TTL:
        return cached[1]
    # The WHERE clause only references f.* (verified across all filter
    # branches), so the JOIN groups is dead weight here. GROUP BY agg over
    # the (file_name, file_size) index is ~5× faster than COUNT(DISTINCT (a,b))
    # which forces a sort.
    total = await _qval(
        f"""SELECT COUNT(*) FROM (
              SELECT 1 FROM files f
              {where}
              GROUP BY f.file_name, f.file_size
            ) t""",
        *c_args,
    )
    total = int(total or 0)
    _DEDUPE_COUNT_CACHE[key] = (now, total)
    if len(_DEDUPE_COUNT_CACHE) > 64:
        # Bound the cache; under default load we have <10 distinct keys.
        oldest = min(_DEDUPE_COUNT_CACHE, key=lambda k: _DEDUPE_COUNT_CACHE[k][0])
        _DEDUPE_COUNT_CACHE.pop(oldest, None)
    return total


_LINK_SORT_COLS = {
    "date":     "l.date",
    "url":      "l.url",
    "platform": "l.platform",
    "files":    "l.file_count",
    "group":    "g.name",
    "context":  "l.context",
}


async def search_links(
    query: str = "",
    platform: Optional[str] = None,
    group_id: Optional[int] = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    show_dead: bool = False,
    dedupe: bool = True,
    url_filter: str = "",
    context_filter: str = "",
    group_filter: str = "",
    min_files: Optional[int] = None,
    max_files: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Tuple[List[Dict], int]:
    direction = _SORT_DIRS.get(sort_dir, "DESC")
    sort_col  = _LINK_SORT_COLS.get(sort_by, "l.date")
    # Outer ORDER BY (dedupe path) refers to projected columns.
    sort_col_outer = sort_col.replace("l.", "d.").replace("g.name", "d.group_name")

    conditions: List[str] = []
    args: List[Any] = []
    idx = 1

    if query:
        conditions.append(f"(l.url ILIKE ${idx} OR l.context ILIKE ${idx})")
        args.append(f"%{query}%"); idx += 1
    if platform:
        conditions.append(f"l.platform = ${idx}"); args.append(platform); idx += 1
    if group_id is not None:
        conditions.append(f"l.group_id = ${idx}"); args.append(group_id); idx += 1
    if url_filter:
        conditions.append(f"l.url ILIKE ${idx}"); args.append(f"%{url_filter}%"); idx += 1
    if context_filter:
        conditions.append(f"l.context ILIKE ${idx}")
        args.append(f"%{context_filter}%"); idx += 1
    if group_filter:
        conditions.append(
            f"(COALESCE(g.display_name, g.name) ILIKE ${idx} OR g.username ILIKE ${idx})"
        )
        args.append(f"%{group_filter}%"); idx += 1
    if min_files is not None:
        conditions.append(f"l.file_count >= ${idx}"); args.append(int(min_files)); idx += 1
    if max_files is not None:
        conditions.append(f"l.file_count <= ${idx}"); args.append(int(max_files)); idx += 1
    def _parse_date(s: str):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None
    df = _parse_date(date_from) if date_from else None
    dt = _parse_date(date_to)   if date_to   else None
    if df:
        conditions.append(f"l.date >= ${idx}"); args.append(df); idx += 1
    if dt:
        conditions.append(f"l.date < ${idx}")
        args.append(dt + timedelta(days=1)); idx += 1
    if not show_dead:
        # Only hide links the prober has confirmed dead. NULL (not yet
        # probed) and TRUE (alive) are both kept visible.
        conditions.append("(l.available IS NULL OR l.available = TRUE)")

    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    c_args = list(args)

    if not dedupe:
        # Raw view — every (group_id, message_id, url) row is its own line.
        args += [limit, offset]
        rows = await _q(
            f"""SELECT l.id, l.group_id, l.message_id, l.platform, l.url, l.context, l.date,
                       l.available, l.file_count, l.file_size_total, l.files_json, l.probed_at,
                       1 AS appearances,
                       COALESCE(g.display_name, g.name) AS group_name,
                       g.username AS group_username
                FROM links l
                JOIN groups g ON g.id = l.group_id
                {where}
                ORDER BY {sort_col} {direction}, l.id {direction}
                LIMIT ${idx} OFFSET ${idx+1}""",
            *args,
        )
        total = await _qval(
            f"SELECT COUNT(*) FROM links l JOIN groups g ON g.id = l.group_id {where}",
            *c_args,
        )
        return [dict(r) for r in rows], total or 0

    # Dedupe by URL: keep one canonical row per URL (the freshest, with all
    # its probe metadata) plus an `appearances` count showing how many
    # underlying rows exist for that URL.
    args += [limit, offset]
    rows = await _q(
        f"""WITH d AS (
                SELECT DISTINCT ON (l.url)
                    l.id, l.group_id, l.message_id, l.platform, l.url, l.context, l.date,
                    l.available, l.file_count, l.file_size_total, l.files_json, l.probed_at,
                    COALESCE(g.display_name, g.name) AS group_name,
                    g.username AS group_username
                FROM links l
                JOIN groups g ON g.id = l.group_id
                {where}
                ORDER BY l.url, l.date DESC
            ),
            c AS (
                SELECT l.url, COUNT(*)::int AS appearances
                FROM links l
                JOIN groups g ON g.id = l.group_id
                {where}
                GROUP BY l.url
            )
            SELECT d.*, c.appearances
            FROM d JOIN c ON c.url = d.url
            ORDER BY {sort_col_outer} {direction}, d.id {direction}
            LIMIT ${idx} OFFSET ${idx+1}""",
        *args,
    )
    total = await _qval(
        f"SELECT COUNT(DISTINCT l.url) FROM links l JOIN groups g ON g.id = l.group_id {where}",
        *c_args,
    )
    return [dict(r) for r in rows], total or 0


async def get_file_by_id(file_id: int) -> Optional[Dict]:
    row = await _qrow(
        """SELECT f.*, COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
           FROM files f
           JOIN groups g ON g.id = f.group_id
           WHERE f.id = $1""",
        file_id,
    )
    return dict(row) if row else None


async def set_file_local_path(file_id: int, local_path: str):
    await _exec(
        """UPDATE files SET local_path=$1, downloaded_at=$2,
           downloading=FALSE, download_progress=1.0
           WHERE id=$3""",
        local_path, datetime.utcnow(), file_id,
    )


async def clear_file_local_path(file_id: int):
    await _exec(
        """UPDATE files SET local_path=NULL, downloaded_at=NULL,
           downloading=FALSE, download_progress=0.0
           WHERE id=$1""",
        file_id,
    )


async def list_downloaded_files() -> List[Dict]:
    rows = await _q(
        """SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
                  f.mime_type, f.file_size, f.date, f.local_path,
                  f.downloaded_at,
                  COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
           FROM files f
           JOIN groups g ON g.id = f.group_id
           WHERE f.local_path IS NOT NULL
           ORDER BY f.downloaded_at DESC NULLS LAST"""
    )
    return [dict(r) for r in rows]


async def set_file_downloading(file_id: int, downloading: bool, progress: float = 0.0):
    await _exec(
        "UPDATE files SET downloading=$1, download_progress=$2 WHERE id=$3",
        downloading, progress, file_id,
    )


async def get_status_stats() -> Dict:
    # File type breakdown
    parts, args = [], []
    for i, (grp, exts) in enumerate(_EXT_GROUPS.items(), 1):
        parts.append(f"WHEN LOWER(file_ext) = ANY(${i}::text[]) THEN '{grp}'")
        args.append(exts)
    by_type = await _q(
        f"""SELECT CASE {' '.join(parts)} ELSE 'other' END AS grp,
                   COUNT(*)                                                      AS cnt,
                   COALESCE(SUM(file_size),0)                                    AS total_sz,
                   COUNT(*) FILTER (WHERE local_path IS NOT NULL)                AS dl_cnt,
                   COALESCE(SUM(file_size) FILTER (WHERE local_path IS NOT NULL),0) AS dl_sz
            FROM files GROUP BY grp ORDER BY cnt DESC""",
        *args,
    )

    recent = await _qrow(
        """SELECT COUNT(*) FILTER (WHERE date >= NOW()-INTERVAL '24 hours') AS h24,
                  COUNT(*) FILTER (WHERE date >= NOW()-INTERVAL '7 days')  AS h7d,
                  COUNT(*) FILTER (WHERE date >= NOW()-INTERVAL '30 days') AS h30d
           FROM files"""
    )

    by_platform = await _q(
        """SELECT COALESCE(platform,'other') AS platform, COUNT(*) AS cnt
           FROM links GROUP BY platform ORDER BY cnt DESC"""
    )

    grp_stats = await _qrow(
        """SELECT COUNT(*)                              AS total,
                  COUNT(*) FILTER (WHERE excluded)     AS excluded,
                  COUNT(*) FILTER (WHERE hidden)       AS hidden,
                  COUNT(*) FILTER (WHERE last_synced_message_id > 0) AS synced
           FROM groups"""
    )

    pg_tables = await _q(
        """SELECT relname                                         AS tablename,
                  pg_size_pretty(pg_total_relation_size(relid))  AS size_pretty,
                  pg_total_relation_size(relid)                  AS size_bytes,
                  pg_size_pretty(pg_relation_size(relid))        AS table_size_pretty,
                  pg_size_pretty(pg_indexes_size(relid))         AS index_size_pretty,
                  n_live_tup                                     AS row_count
           FROM pg_stat_user_tables
           WHERE schemaname = 'public'
           ORDER BY size_bytes DESC"""
    )

    pg_size = await _qrow(
        "SELECT pg_database_size(current_database()) AS bytes, "
        "pg_size_pretty(pg_database_size(current_database())) AS pretty"
    )

    return {
        "by_type":     [dict(r) for r in by_type],
        "recent_24h":  recent["h24"]  if recent else 0,
        "recent_7d":   recent["h7d"]  if recent else 0,
        "recent_30d":  recent["h30d"] if recent else 0,
        "by_platform": [dict(r) for r in by_platform],
        "groups":      dict(grp_stats) if grp_stats else {},
        "pg_tables":   [dict(r) for r in pg_tables],
        "pg_db_size":       int(pg_size["bytes"])  if pg_size else 0,
        "pg_db_size_pretty": pg_size["pretty"] if pg_size else "?",
    }


async def get_stats() -> Dict:
    total_files      = await _qval("SELECT COUNT(*) FROM files")
    downloaded       = await _qval("SELECT COUNT(*) FROM files WHERE local_path IS NOT NULL")
    total_groups     = await _qval("SELECT COUNT(*) FROM groups")
    total_links      = await _qval("SELECT COUNT(*) FROM links")
    max_file_size    = await _qval("SELECT COALESCE(MAX(file_size),0) FROM files")
    excluded_grps    = await _qval("SELECT COUNT(*) FROM groups WHERE excluded=TRUE")
    total_size       = await _qval("SELECT COALESCE(SUM(file_size),0) FROM files")
    downloaded_size  = await _qval("SELECT COALESCE(SUM(file_size),0) FROM files WHERE local_path IS NOT NULL")
    recent           = await _qrow(
        """SELECT COUNT(*) FILTER (WHERE date >= NOW()-INTERVAL '24 hours')                        AS cnt24,
                  COALESCE(SUM(file_size) FILTER (WHERE date >= NOW()-INTERVAL '24 hours'),0)      AS sz24,
                  COUNT(*) FILTER (WHERE date >= NOW()-INTERVAL '7 days')                          AS cnt7,
                  COALESCE(SUM(file_size) FILTER (WHERE date >= NOW()-INTERVAL '7 days'),0)        AS sz7
           FROM files"""
    )
    return {
        "total_files":     total_files or 0,
        "downloaded":      downloaded or 0,
        "total_groups":    total_groups or 0,
        "total_links":     total_links or 0,
        "max_file_size":   max_file_size or 0,
        "excluded_groups": excluded_grps or 0,
        "total_size":      int(total_size or 0),
        "downloaded_size": int(downloaded_size or 0),
        "recent_24h":      int(recent["cnt24"] or 0) if recent else 0,
        "recent_24h_size": int(recent["sz24"]  or 0) if recent else 0,
        "recent_7d":       int(recent["cnt7"]  or 0) if recent else 0,
        "recent_7d_size":  int(recent["sz7"]   or 0) if recent else 0,
    }


# ── Watch terms & notifications ───────────────────────────────────────────────

async def list_watches() -> List[Dict]:
    rows = await _q(
        """SELECT w.id, w.keywords, w.created_at, w.baseline_file_id, w.last_checked_file_id,
                  (SELECT n.id FROM watch_notifications n
                   WHERE n.watch_id = w.id AND n.dismissed_at IS NULL
                   ORDER BY n.first_match_at DESC LIMIT 1) AS active_notification_id,
                  (SELECT COALESCE(array_length(n.file_ids, 1), 0) FROM watch_notifications n
                   WHERE n.watch_id = w.id AND n.dismissed_at IS NULL
                   ORDER BY n.first_match_at DESC LIMIT 1) AS active_match_count,
                  (SELECT n.last_match_at FROM watch_notifications n
                   WHERE n.watch_id = w.id AND n.dismissed_at IS NULL
                   ORDER BY n.first_match_at DESC LIMIT 1) AS active_last_match_at
           FROM watch_terms w
           ORDER BY w.created_at DESC"""
    )
    return [dict(r) for r in rows]


async def create_watch(keywords: str) -> int:
    max_id = await _qval("SELECT COALESCE(MAX(id), 0) FROM files") or 0
    row = await _qrow(
        """INSERT INTO watch_terms (keywords, baseline_file_id, last_checked_file_id)
           VALUES ($1, $2, $2) RETURNING id""",
        keywords, max_id,
    )
    return row["id"]


async def delete_watch(watch_id: int):
    await _exec("DELETE FROM watch_terms WHERE id = $1", watch_id)


async def check_watches() -> int:
    """For each watch term, find new matching files since last check and accumulate
    them into the active notification (creating one if none exists). Returns total
    new file matches across all watches."""
    watches = await _q("SELECT id, keywords, last_checked_file_id FROM watch_terms")
    if not watches:
        return 0

    cur_max = await _qval("SELECT COALESCE(MAX(id), 0) FROM files") or 0
    total_new = 0

    for w in watches:
        keywords = (w["keywords"] or "").strip()
        if not keywords:
            continue
        terms = [t.strip() for t in keywords.replace(",", " ").split() if t.strip()]
        if not terms:
            continue

        # Watch semantics: ALL keywords must appear in the FILE NAME (AND).
        # Each term contributes one ILIKE condition over file_name only.
        conds: List[str] = []
        args: List = [w["last_checked_file_id"]]
        idx = 2
        for t in terms:
            conds.append(f"file_name ILIKE ${idx}")
            args.append(f"%{t}%")
            idx += 1

        sql = (
            "SELECT id FROM files "
            f"WHERE id > $1 AND ({' AND '.join(conds)}) "
            "ORDER BY id ASC"
        )
        rows = await _q(sql, *args)
        new_ids = [int(r["id"]) for r in rows]

        if new_ids:
            existing = await _qrow(
                """SELECT id FROM watch_notifications
                   WHERE watch_id = $1 AND dismissed_at IS NULL
                   ORDER BY first_match_at DESC LIMIT 1""",
                w["id"],
            )
            if existing:
                await _exec(
                    """UPDATE watch_notifications
                       SET file_ids = file_ids || $1::bigint[],
                           last_match_at = NOW()
                       WHERE id = $2""",
                    new_ids, existing["id"],
                )
            else:
                await _exec(
                    "INSERT INTO watch_notifications (watch_id, file_ids) VALUES ($1, $2)",
                    w["id"], new_ids,
                )
            total_new += len(new_ids)

        await _exec(
            "UPDATE watch_terms SET last_checked_file_id = $1 WHERE id = $2",
            cur_max, w["id"],
        )

    return total_new


async def list_active_notifications() -> List[Dict]:
    rows = await _q(
        """SELECT n.id, n.watch_id, n.file_ids, n.first_match_at, n.last_match_at,
                  w.keywords,
                  COALESCE(array_length(n.file_ids, 1), 0) AS match_count,
                  COALESCE((
                      SELECT array_agg(DISTINCT COALESCE(g.display_name, g.name)
                                       ORDER BY COALESCE(g.display_name, g.name))
                      FROM files f JOIN groups g ON g.id = f.group_id
                      WHERE f.id = ANY(n.file_ids)
                  ), ARRAY[]::text[]) AS group_names
           FROM watch_notifications n
           JOIN watch_terms w ON w.id = n.watch_id
           WHERE n.dismissed_at IS NULL
           ORDER BY n.last_match_at DESC"""
    )
    return [dict(r) for r in rows]


async def list_all_notifications(limit: int = 200) -> List[Dict]:
    rows = await _q(
        """SELECT n.id, n.watch_id, n.file_ids, n.first_match_at, n.last_match_at, n.dismissed_at,
                  w.keywords,
                  COALESCE(array_length(n.file_ids, 1), 0) AS match_count,
                  COALESCE((
                      SELECT array_agg(DISTINCT COALESCE(g.display_name, g.name)
                                       ORDER BY COALESCE(g.display_name, g.name))
                      FROM files f JOIN groups g ON g.id = f.group_id
                      WHERE f.id = ANY(n.file_ids)
                  ), ARRAY[]::text[]) AS group_names
           FROM watch_notifications n
           JOIN watch_terms w ON w.id = n.watch_id
           ORDER BY n.last_match_at DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def dismiss_notification(notification_id: int):
    await _exec(
        "UPDATE watch_notifications SET dismissed_at = NOW() WHERE id = $1 AND dismissed_at IS NULL",
        notification_id,
    )


# ── Accounts ──────────────────────────────────────────────────────────────────

async def list_accounts() -> List[Dict]:
    rows = await _q(
        """SELECT a.id, a.name, a.api_id, a.api_hash, a.phone, a.display_name,
                  a.created_at, a.is_active,
                  (SELECT COUNT(*) FROM account_groups ag WHERE ag.account_id = a.id) AS group_count,
                  (SELECT COUNT(*) FROM files f WHERE f.discovered_by_account_id = a.id) AS file_count
           FROM accounts a
           ORDER BY a.id"""
    )
    return [dict(r) for r in rows]


async def get_account(account_id: int) -> Optional[Dict]:
    row = await _qrow("SELECT * FROM accounts WHERE id = $1", account_id)
    return dict(row) if row else None


async def create_account(name: str, api_id: int, api_hash: str) -> int:
    row = await _qrow(
        "INSERT INTO accounts (name, api_id, api_hash) VALUES ($1, $2, $3) RETURNING id",
        name, api_id, api_hash,
    )
    return row["id"]


async def update_account(account_id: int, *, name: Optional[str] = None,
                          api_id: Optional[int] = None, api_hash: Optional[str] = None,
                          phone: Optional[str] = None, display_name: Optional[str] = None,
                          is_active: Optional[bool] = None):
    parts = []
    args: List[Any] = []
    idx = 1
    for col, val in [("name", name), ("api_id", api_id), ("api_hash", api_hash),
                     ("phone", phone), ("display_name", display_name), ("is_active", is_active)]:
        if val is not None:
            parts.append(f"{col} = ${idx}"); args.append(val); idx += 1
    if not parts:
        return
    args.append(account_id)
    await _exec(f"UPDATE accounts SET {', '.join(parts)} WHERE id = ${idx}", *args)


async def delete_account(account_id: int):
    await _exec("DELETE FROM accounts WHERE id = $1", account_id)


# ── Per-account group state ───────────────────────────────────────────────────

async def upsert_account_group(account_id: int, group_id: int):
    """Ensure an account_groups row exists for this (account, group) pair."""
    await _exec(
        """INSERT INTO account_groups (account_id, group_id)
           VALUES ($1, $2)
           ON CONFLICT (account_id, group_id) DO NOTHING""",
        account_id, group_id,
    )


async def find_account_group_by_username(account_id: int, username: str) -> Optional[Dict]:
    """Return {id, name, username} for the group this account is already in
    that matches the given username (case-insensitive). None if not joined."""
    if not username:
        return None
    row = await _qrow(
        """SELECT g.id, g.name, g.username
           FROM groups g
           JOIN account_groups ag ON ag.group_id = g.id
           WHERE LOWER(g.username) = LOWER($1) AND ag.account_id = $2
           LIMIT 1""",
        username, account_id,
    )
    return dict(row) if row else None


async def get_groups_for_account(account_id: int) -> List[Dict]:
    rows = await _q(
        """SELECT g.id, g.name, g.username, g.is_channel,
                  COALESCE(ag.excluded, FALSE) AS excluded,
                  COALESCE(ag.hidden, FALSE)   AS hidden,
                  COALESCE(ag.display_name, g.name) AS display_name,
                  ag.last_synced_at,
                  COUNT(f.id) FILTER (WHERE f.discovered_by_account_id = $1) AS file_count,
                  COALESCE(SUM(f.file_size) FILTER (WHERE f.discovered_by_account_id = $1), 0) AS total_size
           FROM account_groups ag
           JOIN groups g ON g.id = ag.group_id
           LEFT JOIN files f ON f.group_id = g.id
           WHERE ag.account_id = $1
           GROUP BY g.id, ag.excluded, ag.hidden, ag.display_name, ag.last_synced_at
           ORDER BY g.name""",
        account_id,
    )
    return [dict(r) for r in rows]


async def get_excluded_group_ids_for_account(account_id: int) -> List[int]:
    rows = await _q(
        "SELECT group_id FROM account_groups WHERE account_id = $1 AND excluded = TRUE",
        account_id,
    )
    return [r["group_id"] for r in rows]


async def set_account_group_settings(
    account_id: int,
    group_id: int,
    *,
    display_name: Optional[str] = None,
    excluded: Optional[int] = None,
    hidden: Optional[int] = None,
):
    # Ensure row exists first
    await upsert_account_group(account_id, group_id)
    parts = []
    args: List[Any] = []
    idx = 1
    if display_name is not None:
        parts.append(f"display_name = ${idx}"); args.append(display_name); idx += 1
    if excluded is not None:
        parts.append(f"excluded = ${idx}"); args.append(bool(excluded)); idx += 1
    if hidden is not None:
        parts.append(f"hidden = ${idx}"); args.append(bool(hidden)); idx += 1
    if not parts:
        return
    args.extend([account_id, group_id])
    await _exec(
        f"UPDATE account_groups SET {', '.join(parts)} WHERE account_id = ${idx} AND group_id = ${idx+1}",
        *args,
    )


async def get_last_synced_message_id_for_account(account_id: int, group_id: int) -> int:
    v = await _qval(
        "SELECT last_synced_message_id FROM account_groups WHERE account_id = $1 AND group_id = $2",
        account_id, group_id,
    )
    return v or 0


async def update_last_synced_for_account(account_id: int, group_id: int, message_id: int):
    await upsert_account_group(account_id, group_id)
    await _exec(
        """UPDATE account_groups SET last_synced_message_id = $1, last_synced_at = $2
           WHERE account_id = $3 AND group_id = $4""",
        message_id, datetime.utcnow(), account_id, group_id,
    )


async def get_last_synced_link_id_for_account(account_id: int, group_id: int) -> int:
    v = await _qval(
        "SELECT last_link_message_id FROM account_groups WHERE account_id = $1 AND group_id = $2",
        account_id, group_id,
    )
    return v or 0


async def update_last_synced_links_for_account(account_id: int, group_id: int, message_id: int):
    await upsert_account_group(account_id, group_id)
    await _exec(
        """UPDATE account_groups SET last_link_message_id = $1
           WHERE account_id = $2 AND group_id = $3""",
        message_id, account_id, group_id,
    )


async def reset_account_group_watermark(account_id: int, group_id: int):
    await _exec(
        """UPDATE account_groups
           SET last_synced_message_id = 0, last_link_message_id = 0
           WHERE account_id = $1 AND group_id = $2""",
        account_id, group_id,
    )


# ── Hunter (channel discovery) ────────────────────────────────────────────────

DEFAULT_HUNTER_SETTINGS = {
    "id": 1, "enabled": False,
    "stage1_enabled": True, "stage2_enabled": True,
    "web_request_delay_ms": 2500, "web_concurrency": 2,
    "tg_concurrency": 1, "tg_request_delay_ms": 1500,
    "tg_daily_lookup_cap": 500, "tg_messages_to_sample": 200,
    "tg_account_id": 1,
    "schedule_kind": "manual", "schedule_interval_seconds": 86400,
    "keywords": "", "min_subscribers": 0, "languages": "",
    "sources": "",  # empty = run every adapter registered in hunter._STAGE2_SOURCES
    "tg_temp_join_enabled": True,
    # Timestamps written by the scheduler / runner. Kept here so
    # update_hunter_settings() will actually persist them — without these
    # entries the scheduler's next_run_at write was silently dropped and the
    # loop kicked a new run every 60 seconds.
    "last_run_at": None,
    "next_run_at": None,
}


async def get_hunter_settings() -> Dict:
    row = await _qrow("SELECT * FROM hunter_settings WHERE id = 1")
    if not row:
        # Seed default row
        cols = ",".join(DEFAULT_HUNTER_SETTINGS.keys())
        ph = ",".join(f"${i+1}" for i in range(len(DEFAULT_HUNTER_SETTINGS)))
        await _exec(
            f"INSERT INTO hunter_settings ({cols}) VALUES ({ph}) ON CONFLICT (id) DO NOTHING",
            *DEFAULT_HUNTER_SETTINGS.values(),
        )
        row = await _qrow("SELECT * FROM hunter_settings WHERE id = 1")
    return dict(row) if row else dict(DEFAULT_HUNTER_SETTINGS)


async def update_hunter_settings(patch: Dict):
    if not patch:
        return
    parts, args, idx = [], [], 1
    allowed = set(DEFAULT_HUNTER_SETTINGS.keys()) - {"id"}
    dropped = []
    for k, v in patch.items():
        if k not in allowed:
            dropped.append(k)
            continue
        parts.append(f"{k} = ${idx}")
        args.append(v)
        idx += 1
    if dropped:
        # Surface the silent-drop instead of letting it become a heisenbug
        import logging as _logging
        _logging.getLogger("hunter").warning(
            f"update_hunter_settings: dropped unknown key(s): {dropped}"
        )
    if not parts:
        return
    await _qrow(
        f"INSERT INTO hunter_settings (id) VALUES (1) ON CONFLICT DO NOTHING"
    )
    await _exec(f"UPDATE hunter_settings SET {', '.join(parts)} WHERE id = 1", *args)


async def upsert_hunter_candidate(username: str) -> int:
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return 0
    row = await _qrow(
        """INSERT INTO hunter_candidates (username) VALUES ($1)
           ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username
           RETURNING id""",
        u,
    )
    return int(row["id"]) if row else 0


async def add_hunter_source(candidate_id: int, source: str, detail: Optional[str] = None):
    if not candidate_id:
        return
    try:
        await _exec(
            """INSERT INTO hunter_sources (candidate_id, source, detail) VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""",
            candidate_id, source, detail,
        )
    except Exception:
        pass


async def is_blacklisted(username: str) -> bool:
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return False
    v = await _qval("SELECT 1 FROM hunter_blacklist WHERE username = $1", u)
    return bool(v)


async def add_to_blacklist(username: str, reason: Optional[str] = None):
    u = (username or "").strip().lstrip("@").lower()
    await _exec(
        "INSERT INTO hunter_blacklist (username, reason) VALUES ($1, $2) ON CONFLICT (username) DO NOTHING",
        u, reason,
    )


async def remove_from_blacklist(username: str):
    u = (username or "").strip().lstrip("@").lower()
    await _exec("DELETE FROM hunter_blacklist WHERE username = $1", u)


async def list_blacklist() -> List[Dict]:
    rows = await _q("SELECT username, reason, added_at FROM hunter_blacklist ORDER BY added_at DESC")
    return [dict(r) for r in rows]


# Statuses we hide from the default candidate listing — once the user has
# already decided about a candidate (joined / rejected / blacklisted) it
# shouldn't keep cluttering the review queue. Pick the explicit status from
# the UI dropdown to see it again.
_HUNTER_DECIDED_STATUSES = ("joined", "rejected", "blacklisted")


async def list_hunter_candidates(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "score",
) -> Tuple[List[Dict], int]:
    sort_col = {"score": "score DESC NULLS LAST", "discovered_at": "discovered_at DESC",
                "members": "members DESC NULLS LAST"}.get(sort, "score DESC NULLS LAST")
    where = ""
    args: List[Any] = []
    if status:
        # Caller explicitly asked for one bucket — give them only that.
        where = "WHERE status = $1"
        args.append(status)
    else:
        # Default review queue: hide anything the user has already triaged.
        where = "WHERE (status IS NULL OR status <> ALL($1::text[]))"
        args.append(list(_HUNTER_DECIDED_STATUSES))
    total = await _qval(f"SELECT COUNT(*) FROM hunter_candidates {where}", *args) or 0
    args.extend([limit, offset])
    rows = await _q(
        f"""SELECT c.*, (SELECT array_agg(DISTINCT s.source ORDER BY s.source)
                        FROM hunter_sources s WHERE s.candidate_id = c.id) AS sources,
                   q.due_at        AS queue_due_at,
                   q.attempts      AS queue_attempts,
                   q.last_error    AS queue_last_error,
                   EXISTS (
                     SELECT 1 FROM groups g
                     JOIN account_groups ag ON ag.group_id = g.id
                     WHERE LOWER(g.username) = LOWER(c.username)
                   ) AS already_joined
            FROM hunter_candidates c
            LEFT JOIN hunter_join_queue q ON q.candidate_id = c.id
            {where}
            ORDER BY {sort_col}
            LIMIT ${len(args)-1} OFFSET ${len(args)}""",
        *args,
    )
    return [dict(r) for r in rows], int(total)


# ── Join queue (FloodWait retry) ─────────────────────────────────────────────

async def enqueue_join(candidate_id: int, account_id: int, wait_seconds: int,
                        last_error: Optional[str] = None):
    """Insert or refresh a queue entry. attempts increments on every retry."""
    due_at = datetime.utcnow() + timedelta(seconds=max(1, int(wait_seconds)))
    await _exec(
        """INSERT INTO hunter_join_queue (candidate_id, account_id, due_at, last_error, attempts, queued_at)
           VALUES ($1, $2, $3, $4, 1, NOW())
           ON CONFLICT (candidate_id) DO UPDATE SET
             due_at          = EXCLUDED.due_at,
             last_error      = EXCLUDED.last_error,
             attempts        = hunter_join_queue.attempts + 1,
             last_attempt_at = NOW(),
             account_id      = EXCLUDED.account_id""",
        candidate_id, account_id, due_at, last_error,
    )


async def list_due_joins(limit: int = 50) -> List[Dict]:
    rows = await _q(
        """SELECT candidate_id, account_id, due_at, attempts
           FROM hunter_join_queue
           WHERE due_at <= NOW()
           ORDER BY due_at
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def delete_join_from_queue(candidate_id: int):
    await _exec("DELETE FROM hunter_join_queue WHERE candidate_id = $1", candidate_id)


async def list_join_queue() -> List[Dict]:
    rows = await _q(
        """SELECT q.candidate_id, q.account_id, q.due_at, q.attempts, q.last_error,
                  c.username
           FROM hunter_join_queue q
           JOIN hunter_candidates c ON c.id = q.candidate_id
           ORDER BY q.due_at"""
    )
    return [dict(r) for r in rows]


async def get_hunter_candidate(candidate_id: int) -> Optional[Dict]:
    row = await _qrow(
        """SELECT c.*, (SELECT array_agg(DISTINCT s.source ORDER BY s.source)
                        FROM hunter_sources s WHERE s.candidate_id = c.id) AS sources
           FROM hunter_candidates c WHERE c.id = $1""",
        candidate_id,
    )
    return dict(row) if row else None


async def update_hunter_candidate(candidate_id: int, patch: Dict):
    if not patch:
        return
    parts, args, idx = [], [], 1
    allowed = {"title","description","is_channel","members","language","sampled_messages",
               "file_count_sample","estimated_files","avg_file_size","last_message_at",
               "file_type_breakdown","score","status","enriched_at","decided_at","error",
               "peer_id","access_hash",
               "deep_scan_status","deep_scan_progress","deep_scan_total","deep_scan_at","deep_scan_error"}
    for k, v in patch.items():
        if k not in allowed:
            continue
        # Cast JSONB explicitly
        if k == "file_type_breakdown":
            parts.append(f"{k} = ${idx}::jsonb")
        else:
            parts.append(f"{k} = ${idx}")
        args.append(v)
        idx += 1
    if not parts:
        return
    args.append(candidate_id)
    await _exec(f"UPDATE hunter_candidates SET {', '.join(parts)} WHERE id = ${idx}", *args)


async def list_hunter_runs(limit: int = 30) -> List[Dict]:
    rows = await _q(
        "SELECT * FROM hunter_runs ORDER BY started_at DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


async def start_hunter_run(note: str = "") -> int:
    row = await _qrow(
        "INSERT INTO hunter_runs (note) VALUES ($1) RETURNING id",
        note,
    )
    return int(row["id"]) if row else 0


async def finish_hunter_run(run_id: int, *, seeds_found: int = 0, enriched: int = 0,
                              failed: int = 0, error: Optional[str] = None):
    await _exec(
        """UPDATE hunter_runs SET finished_at = NOW(),
                                  seeds_found = $1, enriched = $2,
                                  failed = $3, error = $4
           WHERE id = $5""",
        seeds_found, enriched, failed, error, run_id,
    )


async def hunter_lookups_today() -> int:
    """Approximate: count enriched candidates whose enriched_at is today (UTC)."""
    v = await _qval(
        "SELECT COUNT(*) FROM hunter_candidates WHERE enriched_at >= (NOW() AT TIME ZONE 'UTC')::date"
    )
    return int(v or 0)


# ── Hunter candidate full file list & failed cleanup ─────────────────────────

async def insert_candidate_file(candidate_id: int, message_id: int, file_name: Optional[str],
                                 file_ext: Optional[str], file_size: int,
                                 file_group: Optional[str], date) -> bool:
    try:
        await _exec(
            """INSERT INTO hunter_candidate_files
               (candidate_id, message_id, file_name, file_ext, file_size, file_group, date)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (candidate_id, message_id) DO NOTHING""",
            candidate_id, message_id, file_name, file_ext, file_size, file_group, date,
        )
        return True
    except Exception:
        return False


async def list_candidate_files(candidate_id: int, *, q: str = "", ext: str = "",
                                sort_by: str = "date", sort_dir: str = "desc",
                                limit: int = 200, offset: int = 0) -> Tuple[List[Dict], int]:
    cols = {"date": "date", "name": "file_name", "size": "file_size", "ext": "file_ext"}
    col = cols.get(sort_by, "date")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    where = ["candidate_id = $1"]
    args: List[Any] = [candidate_id]
    idx = 2
    if q:
        where.append(f"file_name ILIKE ${idx}"); args.append(f"%{q}%"); idx += 1
    if ext:
        where.append(f"LOWER(file_ext) = LOWER(${idx})"); args.append(ext.lstrip(".")); idx += 1
    wsql = " AND ".join(where)
    total = await _qval(f"SELECT COUNT(*) FROM hunter_candidate_files WHERE {wsql}", *args) or 0
    args.extend([limit, offset])
    rows = await _q(
        f"""SELECT message_id, file_name, file_ext, file_size, file_group, date
            FROM hunter_candidate_files
            WHERE {wsql}
            ORDER BY {col} {direction} NULLS LAST
            LIMIT ${len(args)-1} OFFSET ${len(args)}""",
        *args,
    )
    return [dict(r) for r in rows], int(total)


async def candidate_file_summary(candidate_id: int) -> Dict:
    row = await _qrow(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(file_size), 0) AS total_size,
                  COALESCE(AVG(file_size), 0) AS avg_size,
                  MAX(date) AS last_date,
                  MIN(date) AS first_date
           FROM hunter_candidate_files WHERE candidate_id = $1""",
        candidate_id,
    )
    summary = dict(row) if row else {}
    # By group
    grp_rows = await _q(
        """SELECT file_group, COUNT(*) AS cnt
           FROM hunter_candidate_files WHERE candidate_id = $1
           GROUP BY file_group""",
        candidate_id,
    )
    summary["by_group"] = {r["file_group"] or "other": r["cnt"] for r in grp_rows}
    return summary


async def delete_hunter_candidate(candidate_id: int):
    await _exec("DELETE FROM hunter_candidates WHERE id = $1", candidate_id)


# ── Telemetry ────────────────────────────────────────────────────────────────

async def get_telemetry_settings() -> Dict:
    import uuid as _uuid
    row = await _qrow("SELECT * FROM telemetry_settings WHERE id = 1")
    if not row:
        install_id = _uuid.uuid4().hex
        await _exec(
            "INSERT INTO telemetry_settings (id, install_id) VALUES (1, $1) "
            "ON CONFLICT (id) DO NOTHING", install_id,
        )
        row = await _qrow("SELECT * FROM telemetry_settings WHERE id = 1")
    return dict(row) if row else {}


async def update_telemetry_settings(patch: Dict):
    if not patch:
        return
    allowed = {"enabled", "endpoint_url", "interval_seconds",
               "last_sent_at", "last_sent_status", "last_sent_error", "next_send_at"}
    parts, args, idx = [], [], 1
    for k, v in patch.items():
        if k not in allowed:
            continue
        parts.append(f"{k} = ${idx}")
        args.append(v); idx += 1
    if not parts:
        return
    await _exec(f"UPDATE telemetry_settings SET {', '.join(parts)} WHERE id = 1", *args)


async def get_groups_for_telemetry() -> List[Dict]:
    """Aggregate channel statistics across ALL accounts: username (or id),
    member_count, total file count. Excluded/hidden groups are still included
    since they are part of the account's view (omitting only ones with zero
    files keeps the payload tight)."""
    rows = await _q(
        """SELECT g.id, g.username, g.is_channel, g.member_count,
                  COUNT(f.id) AS file_count,
                  COALESCE(SUM(f.file_size), 0) AS total_size
           FROM groups g
           LEFT JOIN files f ON f.group_id = g.id
           GROUP BY g.id
           HAVING COUNT(f.id) > 0 OR g.member_count IS NOT NULL
           ORDER BY g.id"""
    )
    return [dict(r) for r in rows]


async def get_groups_needing_member_count(limit: int = 10, refresh_after_days: int = 7) -> List[Dict]:
    rows = await _q(
        f"""SELECT id, username FROM groups
            WHERE (member_count IS NULL
                   OR member_count_updated_at IS NULL
                   OR member_count_updated_at < NOW() - INTERVAL '{refresh_after_days} days')
              AND username IS NOT NULL
            ORDER BY member_count_updated_at NULLS FIRST
            LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def update_group_member_count(group_id: int, count: int):
    await _exec(
        "UPDATE groups SET member_count = $1, member_count_updated_at = NOW() WHERE id = $2",
        count, group_id,
    )
