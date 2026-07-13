import json as _json
import os
import asyncpg
from datetime import datetime, timedelta, timezone
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
    synced_at TIMESTAMPTZ,
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
CREATE INDEX IF NOT EXISTS idx_files_synced_at ON files (synced_at DESC) WHERE synced_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_downloaded ON files (file_size) WHERE local_path IS NOT NULL;
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
    anthropic_api_key TEXT DEFAULT '',
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

-- Per-file local download metadata: when the user clicks 📥 on a row in the
-- candidate detail lightbox, we save the file under DOWNLOADS_DIR/<username>/
-- and store the absolute path here so subsequent UI loads can show "✓ Open".
ALTER TABLE hunter_candidate_files ADD COLUMN IF NOT EXISTS local_path TEXT;
ALTER TABLE hunter_candidate_files ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMPTZ;

ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_status TEXT;          -- NULL | 'queued' | 'running' | 'done' | 'error'
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_progress INTEGER DEFAULT 0;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_total INTEGER DEFAULT 0;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_at TIMESTAMPTZ;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS deep_scan_error TEXT;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS tg_temp_join_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS skip_old_channels BOOLEAN DEFAULT TRUE;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS magnethunt_enabled  BOOLEAN DEFAULT TRUE;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS magnet_backfill_enabled BOOLEAN DEFAULT TRUE;
-- User's UI language (tr|en|de|ru|zh). Backend enrich uses this to decide
-- which non-Latin scripts (CJK/Arabic/etc.) are "acceptable" for the user.
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS ui_language TEXT DEFAULT 'tr';
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS similar_expand_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS similar_expand_max_per_seed INTEGER DEFAULT 10;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS similar_expand_max_seeds INTEGER DEFAULT 100;
-- FloodWait izleme: Stage 3 cached-only mode'a girince mutlak bitiş zamanı +
-- scope + orijinal süre buraya yazılır. /api/hunter/quota bunu okur ve UI
-- kota lightbox'ında "pencere ne zaman kapanıyor / ne kadar kaldı" bilgisini
-- gösterir.
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS last_floodwait_until TIMESTAMPTZ;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS last_floodwait_scope TEXT;
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS last_floodwait_seconds INTEGER;
-- Stage 0 tekrar tarama önleme: bir kanal/aday benzer-kanal için sorgulandığında
-- zaman damgası basılır; bir sonraki oturumda bu kayıtlar atlanır.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS stage0_scanned_at TIMESTAMPTZ;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS stage0_scanned_at TIMESTAMPTZ;
-- Otomatik anahtar-kelime havuzu: Stage 3 başarılı zenginleştirmeden sonra
-- kanal açıklamasından / başlığından çıkarılan anlamlı kelimeler buraya
-- birikiyor. Stage 2 bir sonraki koşuda user keywords + bunları birleştiriyor
-- → her keşif sistemin sorgu yüzeyini büyütüyor (#3).
ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS learned_keywords TEXT DEFAULT '';
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS peer_id BIGINT;
ALTER TABLE hunter_candidates ADD COLUMN IF NOT EXISTS access_hash BIGINT;
-- "Adlı dosya" mı, "doğal Telegram medyası" mı (sesli mesaj, kamera
-- videosu, sticker, animasyon vb.) — kullanıcı bunu kanal kalite
-- değerlendirmesinde görmek istiyor. TRUE = DocumentAttributeFilename
-- mevcuttu = gerçek dosya paylaşımı; FALSE = sentetik isim ürettik.
ALTER TABLE hunter_candidate_files ADD COLUMN IF NOT EXISTS is_named BOOLEAN DEFAULT TRUE;

-- Backfill: eski Stage 3 / deep-scan kayıtları DocumentAttributeFilename
-- olmayan Telegram dokümanları için NULL file_name yazıyordu (kameradan
-- yüklenen videolar, sesli mesajlar, taglı audio, animasyonlar). UI bu
-- satırları "—" olarak gösteriyordu. Yeni ingestion artık sentetik isim
-- yazıyor (video_{msg_id}.mp4 vb.) — geçmiş satırları aynı şablonla
-- doldur.
UPDATE hunter_candidate_files
SET file_name = CASE
        WHEN file_group = 'video'    THEN 'video_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '.mp4')
        WHEN file_group = 'audio'    THEN 'audio_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '.mp3')
        WHEN file_group = 'image'    THEN 'image_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '.jpg')
        WHEN file_group = 'archive'  THEN 'archive_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '')
        WHEN file_group = 'document' THEN 'document_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '')
        WHEN file_group = 'software' THEN 'app_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '')
        ELSE                              'file_' || message_id ||
            COALESCE('.' || NULLIF(file_ext, ''), '')
    END,
    is_named = FALSE
WHERE file_name IS NULL OR file_name = '';

-- Daha önce backfill koşmuş kurulumlar için: yapay isim formatına uyan
-- ama is_named hâlâ TRUE olan satırları yakalayıp düzelt. Pattern:
-- (video|audio|image|file|archive|document|app)_<sayı>(\.<ext>)?
UPDATE hunter_candidate_files
SET is_named = FALSE
WHERE is_named = TRUE
  AND file_name ~ '^(video|audio|image|file|archive|document|app)_[0-9]+(\.[A-Za-z0-9]+)?$';

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

-- Singleton row holding watch-related global toggles. Currently used by the
-- "İzlem eşleşmesi → kendi Telegram Saved Messages'a anlık push" özelliği.
CREATE TABLE IF NOT EXISTS notify_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    tg_push_enabled BOOLEAN DEFAULT FALSE,
    last_push_at TIMESTAMPTZ
);
INSERT INTO notify_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS transfer_destinations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bandwidth_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT FALSE,
    min_size_mb INTEGER DEFAULT 0
);
INSERT INTO bandwidth_settings (id, enabled, min_size_mb) VALUES (1, FALSE, 0) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS bandwidth_schedules (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    rule_type TEXT NOT NULL DEFAULT 'weekly',
    days INTEGER[] DEFAULT '{}',
    start_time TEXT NOT NULL DEFAULT '02:00',
    end_time TEXT NOT NULL DEFAULT '06:00',
    specific_date TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scheduled_downloads (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL,
    destination_ids JSONB DEFAULT '[]',
    scheduled_at TIMESTAMPTZ,
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(file_id)
);

CREATE TABLE IF NOT EXISTS torrent_contents (
    file_id BIGINT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    torrent_name TEXT,
    total_size BIGINT DEFAULT 0,
    file_count INTEGER DEFAULT 0,
    tree JSONB DEFAULT '[]',
    parsed_at TIMESTAMPTZ DEFAULT NOW(),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_torrent_contents_parsed ON torrent_contents (parsed_at) WHERE error IS NULL;

-- Normalized table: one row per file inside a .torrent. Enables fast
-- substring search via trigram index without JSONB expansion at query time.
CREATE TABLE IF NOT EXISTS torrent_files (
    id BIGSERIAL PRIMARY KEY,
    torrent_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    size BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tf_torrent ON torrent_files (torrent_id);
CREATE INDEX IF NOT EXISTS idx_tf_path_trgm ON torrent_files USING GIN (path gin_trgm_ops);

-- Tracks which group IDs have already been included in a telemetry payload.
-- Rows are inserted after a successful POST so the same channel is never
-- reported twice.
CREATE TABLE IF NOT EXISTS telemetry_sent_groups (
    group_id BIGINT PRIMARY KEY,
    sent_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Tracks which file IDs have already been sent in a telemetry payload.
-- Files are batched (3 000/payload); rows inserted after a successful POST.
CREATE TABLE IF NOT EXISTS telemetry_sent_files (
    file_id BIGINT PRIMARY KEY,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS link_archive_contents (
    id           SERIAL PRIMARY KEY,
    link_id      BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    archive_path TEXT NOT NULL,
    contents     JSONB NOT NULL DEFAULT '[]',
    inspected_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (link_id, archive_path)
);

-- Versioned migration tracker. Created here so it exists before
-- _run_migrations() is called. Version 0 = everything built by _SCHEMA above.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO schema_migrations (version, description)
VALUES (0, 'baseline — full schema created by _SCHEMA block')
ON CONFLICT DO NOTHING;
"""


# ---------------------------------------------------------------------------
# Versioned migration list
# ---------------------------------------------------------------------------
# Each entry: (version: int, description: str, sql: str)
#
# Rules:
#   • version numbers must be consecutive and never reused.
#   • sql runs inside a single transaction; keep each migration atomic.
#   • _SCHEMA above handles idempotent additions (ADD COLUMN IF NOT EXISTS).
#     Use _MIGRATIONS only for changes that _SCHEMA cannot express safely:
#       - ALTER COLUMN … TYPE …   (type change)
#       - ALTER TABLE … RENAME …  (table or column rename)
#       - DROP TABLE / DROP COLUMN (destructive removals)
#       - Data-shape transforms that must run exactly once
#   • Once a version is shipped, never edit its sql — add a new version.
#   • IMPORTANT — always write migrations defensively so they are safe for
#     both existing installs (schema in old state) AND fresh installs
#     (_SCHEMA already created the final state). Use DO $$ BEGIN … END $$
#     guards that check whether the change is still needed before applying it.
#
# Template for a safe column rename (idempotent on both old and new installs):
#
#   (1, "rename files.context to files.caption",
#    """
#    DO $$ BEGIN
#      IF EXISTS (
#        SELECT 1 FROM information_schema.columns
#        WHERE table_name = 'files' AND column_name = 'context'
#      ) THEN
#        ALTER TABLE files RENAME COLUMN context TO caption;
#      END IF;
#    END $$;
#    """),
#
# Template for a safe column type change:
#
#   (2, "widen files.file_size to NUMERIC",
#    """
#    DO $$ BEGIN
#      IF (SELECT data_type FROM information_schema.columns
#          WHERE table_name = 'files' AND column_name = 'file_size') = 'bigint' THEN
#        ALTER TABLE files ALTER COLUMN file_size TYPE NUMERIC USING file_size::NUMERIC;
#      END IF;
#    END $$;
#    """),
# ---------------------------------------------------------------------------
_MIGRATIONS: list[tuple[int, str, str]] = [
    # future breaking migrations go here
]

# Optional pgvector setup — applied separately so failures (extension not
# installed in Postgres, etc.) degrade gracefully to "semantic search off".
_VECTOR_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE files ADD COLUMN IF NOT EXISTS name_embedding vector(384);
CREATE INDEX IF NOT EXISTS idx_files_name_emb
  ON files USING hnsw (name_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE name_embedding IS NOT NULL;
"""

# Flag toggled at init_db; readers use it to decide if hybrid path is
# even attempted.
_VECTOR_AVAILABLE = False

# Materialized view for the dedupe path. Replaces the per-request window
# function over ~750k rows with an indexed lookup. Refreshed periodically
# and after mutations via refresh_files_canonical().
_MV_SCHEMA = """
-- Drop+recreate so we can add new columns (share_count, share_count_7d,
-- share_count_30d). PostgreSQL has no ALTER MATERIALIZED VIEW ADD COLUMN;
-- DROP IF EXISTS keeps the upgrade path idempotent across restarts.
DROP MATERIALIZED VIEW IF EXISTS files_canonical CASCADE;
CREATE MATERIALIZED VIEW files_canonical AS
WITH share_stats AS (
  SELECT file_name, file_size,
         COUNT(DISTINCT group_id)::int AS share_count,
         COUNT(DISTINCT CASE WHEN COALESCE(synced_at, date) >= NOW() - INTERVAL '7 days'
                              THEN group_id END)::int AS share_count_7d,
         COUNT(DISTINCT CASE WHEN COALESCE(synced_at, date) >= NOW() - INTERVAL '30 days'
                              THEN group_id END)::int AS share_count_30d
  FROM files
  WHERE file_name IS NOT NULL AND file_name <> ''
  GROUP BY file_name, file_size
),
ranked AS (
  SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
         f.mime_type, f.file_size, f.date, f.local_path,
         f.downloading, f.download_progress, f.context,
         {emb_proj_inner}
         COUNT(*) OVER (PARTITION BY f.file_name, f.file_size)::int AS appearances,
         COALESCE(s.share_count, 1)      AS share_count,
         COALESCE(s.share_count_7d, 0)   AS share_count_7d,
         COALESCE(s.share_count_30d, 0)  AS share_count_30d,
         ROW_NUMBER() OVER (
           PARTITION BY f.file_name, f.file_size
           ORDER BY (f.local_path IS NULL), f.date DESC
         ) AS _rn
  FROM files f
  LEFT JOIN share_stats s ON s.file_name = f.file_name AND s.file_size = f.file_size
)
SELECT id, group_id, message_id, file_name, file_ext, mime_type,
       file_size, date, local_path, downloading, download_progress,
       context, {emb_proj_outer}
       appearances, share_count, share_count_7d, share_count_30d
FROM ranked WHERE _rn = 1
WITH NO DATA;

CREATE UNIQUE INDEX IF NOT EXISTS idx_fc_id        ON files_canonical (id);
CREATE INDEX        IF NOT EXISTS idx_fc_date      ON files_canonical (date DESC);
CREATE INDEX        IF NOT EXISTS idx_fc_size      ON files_canonical (file_size DESC);
CREATE INDEX        IF NOT EXISTS idx_fc_ext       ON files_canonical (file_ext);
CREATE INDEX        IF NOT EXISTS idx_fc_group     ON files_canonical (group_id);
CREATE INDEX        IF NOT EXISTS idx_fc_ext_date  ON files_canonical (file_ext, date DESC);
CREATE INDEX        IF NOT EXISTS idx_fc_grp_date  ON files_canonical (group_id, date DESC);
CREATE INDEX        IF NOT EXISTS idx_fc_name_size ON files_canonical (file_name, file_size);
CREATE INDEX        IF NOT EXISTS idx_fc_name_trgm ON files_canonical USING GIN (file_name gin_trgm_ops);
CREATE INDEX        IF NOT EXISTS idx_fc_shares    ON files_canonical (share_count DESC);
"""

# HNSW index on the MV's embedding column, applied separately because
# the column type depends on the optional pgvector extension.
_MV_VECTOR_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fc_name_emb
  ON files_canonical USING hnsw (name_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE name_embedding IS NOT NULL;
"""


async def _register_vector_on_conn(conn):
    """Pool init callback — registers pgvector codecs so we can bind/return
    `vector` typed values from Python. Silent no-op if pgvector lib or the
    extension isn't installed; semantic search degrades, app keeps running."""
    try:
        from pgvector.asyncpg import register_vector
        await register_vector(conn)
    except Exception:
        pass


async def init_db():
    global _pool, _VECTOR_AVAILABLE
    import logging as _lg
    log = _lg.getLogger(__name__)

    # Phase 1: ensure pgvector extension exists AND the Python adapter is
    # importable. Both are needed: the extension provides the `vector` type
    # in Postgres; the adapter teaches asyncpg how to encode/decode it.
    try:
        from pgvector.asyncpg import register_vector as _rv  # noqa: F401
        _have_pgvector_lib = True
    except Exception as e:
        _have_pgvector_lib = False
        log.warning("pgvector Python lib missing (%s) — semantic search disabled.", e)
    if _have_pgvector_lib:
        try:
            _tmp = await asyncpg.connect(DATABASE_URL)
            try:
                await _tmp.execute("CREATE EXTENSION IF NOT EXISTS vector")
                _VECTOR_AVAILABLE = True
                log.info("pgvector extension + adapter ready.")
            except Exception as e:
                log.warning("pgvector extension unavailable (%s) — semantic search disabled.", e)
            finally:
                await _tmp.close()
        except Exception as e:
            log.warning("pre-init connection failed: %s", e)

    _pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10,
        init=_register_vector_on_conn if _VECTOR_AVAILABLE else None,
    )
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
        await _run_migrations(conn, log)
        # Vector column + HNSW index on raw `files` (skipped if extension missing)
        if _VECTOR_AVAILABLE:
            try:
                await conn.execute(_VECTOR_SCHEMA)
            except Exception as e:
                log.warning("vector schema apply failed: %s", e)
                _VECTOR_AVAILABLE = False
        # The MV's DROP+CREATE+REFRESH is ~9 min on a 750k+ row dataset, so
        # we skip it when the existing MV already matches the expected schema
        # (column set + populated flag). Detect drift via the column list —
        # vector column comes/goes with pgvector, share_count_* etc. with code
        # changes — and only rebuild on mismatch.
        _MV_EXPECTED_COLS = {
            "id","group_id","message_id","file_name","file_ext","mime_type",
            "file_size","date","local_path","downloading","download_progress",
            "context","appearances","share_count","share_count_7d","share_count_30d",
        }
        # name_embedding is intentionally excluded from files_canonical even
        # when pgvector is available. Keeping it inline (1542 bytes/row) bloats
        # the MV to 22+ GB after repeated REFRESH CONCURRENTLY cycles, making
        # every Bitmap Heap Scan 5-10× slower. Semantic search uses
        # files.name_embedding (separate HNSW index) joined by id instead.
        existing_rows = await conn.fetch(
            """SELECT a.attname
                 FROM pg_attribute a
                 JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'files_canonical' AND c.relkind = 'm'
                  AND a.attnum > 0 AND NOT a.attisdropped"""
        )
        existing_cols = {r["attname"] for r in existing_rows}
        mv_populated = bool(await conn.fetchval(
            "SELECT relispopulated FROM pg_class WHERE relname = 'files_canonical'"
        ))
        if existing_cols == _MV_EXPECTED_COLS and mv_populated:
            log.info(
                "files_canonical: schema match (%d cols, populated), "
                "skipping rebuild.", len(existing_cols)
            )
        else:
            log.info(
                "files_canonical: rebuilding "
                "(schema_match=%s, was_populated=%s, expected=%d, found=%d)",
                existing_cols == _MV_EXPECTED_COLS, mv_populated,
                len(_MV_EXPECTED_COLS), len(existing_cols),
            )
            mv_sql = _MV_SCHEMA.format(
                emb_proj_inner="",
                emb_proj_outer="",
            )
            await conn.execute(mv_sql)
    await _migrate_to_multi_account()
    # Populate the dedupe MV on first run AND after a schema rebuild (DROP+CREATE
    # leaves the MV in "not populated" state, where SELECT raises). We probe
    # via pg_catalog so we don't trip the not-populated error before deciding
    # whether to refresh.
    try:
        import logging as _lg, time as _t
        log = _lg.getLogger(__name__)
        is_populated = await _qval(
            "SELECT relispopulated FROM pg_class WHERE relname = 'files_canonical'"
        )
        if not is_populated:
            log.info("files_canonical: initial population starting…")
            t0 = _t.time()
            await _exec("REFRESH MATERIALIZED VIEW files_canonical")
            log.info("files_canonical: populated in %.2fs", _t.time() - t0)
    except Exception as e:
        import logging as _lg
        _lg.getLogger(__name__).warning("files_canonical init refresh failed: %s", e)


async def _run_migrations(conn, log) -> None:
    """Apply any pending entries from _MIGRATIONS in version order.

    Each migration runs inside its own transaction. On failure the transaction
    is rolled back, an error is logged, and a RuntimeError is raised so that
    init_db() surfaces the problem at startup rather than letting the app run
    on a broken schema.
    """
    if not _MIGRATIONS:
        return
    applied = {r["version"] for r in await conn.fetch(
        "SELECT version FROM schema_migrations"
    )}
    pending = sorted(
        (v, d, s) for v, d, s in _MIGRATIONS if v not in applied
    )
    if not pending:
        return
    for version, description, sql in pending:
        log.info("DB migration v%d starting: %s", version, description)
        try:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES ($1, $2)",
                    version, description,
                )
            log.info("DB migration v%d applied successfully.", version)
        except Exception as exc:
            log.error(
                "DB migration v%d FAILED (%s). "
                "Fix the migration or restore from backup before restarting.",
                version, exc,
            )
            raise RuntimeError(
                f"Database migration v{version} failed: {exc}"
            ) from exc


async def _migrate_to_multi_account():
    # Add discovered_by_account_id columns if missing (idempotent)
    await _exec("""ALTER TABLE files ADD COLUMN IF NOT EXISTS discovered_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL""")
    await _exec("""ALTER TABLE links ADD COLUMN IF NOT EXISTS discovered_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL""")
    # Add synced_at column if missing; existing rows keep NULL (unknown sync time)
    await _exec("""ALTER TABLE files ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ""")
    # Add anthropic_api_key to hunter_settings if missing
    await _exec("""ALTER TABLE hunter_settings ADD COLUMN IF NOT EXISTS anthropic_api_key TEXT DEFAULT ''""")
    # Add explicit schedule time to scheduled_downloads if missing
    await _exec("ALTER TABLE scheduled_downloads ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ")
    await _exec("ALTER TABLE watch_terms ADD COLUMN IF NOT EXISTS min_size_bytes BIGINT NOT NULL DEFAULT 0")

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


async def ensure_synthetic_group(group_id: int, name: str, display_name: Optional[str] = None) -> None:
    """Insert a synthetic (non-Telegram) group row for storing web-discovered
    links. Uses a negative id so it cannot collide with a real Telegram chat.
    No-op if the row already exists with a matching name."""
    await _exec(
        """INSERT INTO groups (id, name, username, is_channel, display_name)
           VALUES ($1, $2, NULL, FALSE, $3)
           ON CONFLICT (id) DO UPDATE SET
               name = EXCLUDED.name,
               display_name = COALESCE(EXCLUDED.display_name, groups.display_name)""",
        group_id, name, display_name,
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


def _ext_list_sql(group: str) -> str:
    # Render the _EXT_GROUPS[group] list as a parenthesised SQL literal so it
    # can be inlined into a COUNT(... FILTER (WHERE ext IN (...))) expression.
    exts = _EXT_GROUPS.get(group, [])
    return "(" + ",".join("'" + e.replace("'", "''") + "'" for e in exts) + ")" if exts else "(NULL)"


def _ext_list_sql_combined(*groups: str) -> str:
    """Merge multiple extension groups into a single SQL IN-list."""
    all_exts: List[str] = []
    for g in groups:
        all_exts.extend(_EXT_GROUPS.get(g, []))
    return "(" + ",".join("'" + e.replace("'", "''") + "'" for e in all_exts) + ")" if all_exts else "(NULL)"


async def get_groups() -> List[Dict]:
    audio_l  = _ext_list_sql("audio")
    video_l  = _ext_list_sql("video")
    image_l  = _ext_list_sql("image")
    archv_l  = _ext_list_sql("archive")
    docu_l   = _ext_list_sql("document")
    soft_l   = _ext_list_sql("software")
    torr_l   = _ext_list_sql("torrent")
    rows = await _q(
        f"""SELECT g.id, g.name, g.username, g.is_channel,
                  g.excluded, g.hidden,
                  COALESCE(g.display_name, g.name)                AS display_name,
                  g.last_synced_at,
                  g.member_count,
                  g.member_count_updated_at,
                  COUNT(f.id)                                     AS file_count,
                  COALESCE(SUM(f.file_size), 0)                   AS total_size,
                  MAX(f.date)                                     AS last_message_at,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {audio_l}) AS type_audio,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {video_l}) AS type_video,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {image_l}) AS type_image,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {archv_l}) AS type_archive,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {docu_l})  AS type_document,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {soft_l})  AS type_software,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {torr_l})  AS type_torrent,
                  COUNT(f.id) FILTER (WHERE f.file_ext IS NULL OR LOWER(f.file_ext) NOT IN
                      ({audio_l[1:-1]}, {video_l[1:-1]}, {image_l[1:-1]},
                       {archv_l[1:-1]}, {docu_l[1:-1]}, {soft_l[1:-1]}, {torr_l[1:-1]}))
                                                                  AS type_other,
                  hc.score                                        AS hunter_score
           FROM groups g
           LEFT JOIN files f ON f.group_id = g.id
           LEFT JOIN hunter_candidates hc ON LOWER(hc.username) = LOWER(g.username)
           WHERE g.id < 0
              OR EXISTS (SELECT 1 FROM account_groups ag WHERE ag.group_id = g.id)
           GROUP BY g.id, hc.score
           ORDER BY g.name"""
    )
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        d = dict(r)
        if d["hunter_score"] is None:
            # No hunter_candidates row — compute score from actual file data
            useful = (
                (d.get("type_archive") or 0)
                + (d.get("type_document") or 0)
                + (d.get("type_software") or 0)
                + (d.get("type_other") or 0)
            )
            file_count = d.get("file_count") or 0
            last_msg = d.get("last_message_at")
            if useful > 0 and file_count > 0:
                useful_density = min(1.0, useful / file_count)
                if last_msg:
                    if last_msg.tzinfo is None:
                        last_msg = last_msg.replace(tzinfo=timezone.utc)
                    days_since = (now - last_msg).total_seconds() / 86400
                else:
                    days_since = 999.0
                recency = max(0.0, 1.0 - days_since / 60.0)
                d["hunter_score"] = round((0.55 * useful_density + 0.30 * recency) * 100, 2)
            else:
                d["hunter_score"] = 0.0
        result.append(d)
    return result


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
) -> Optional[int]:
    try:
        date_ts = datetime.fromisoformat(date.replace("Z", "+00:00")) if date else datetime.utcnow()
        row = await _qrow(
            """INSERT INTO files
               (group_id, message_id, file_name, file_ext, mime_type, file_size, date, synced_at, context, discovered_by_account_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8,$9)
               RETURNING id""",
            group_id, message_id, file_name, file_ext, mime_type, file_size, date_ts, context, discovered_by_account_id,
        )
        return row["id"] if row else None
    except asyncpg.UniqueViolationError:
        return None


async def insert_link(
    group_id: int,
    message_id: int,
    platform: Optional[str],
    url: str,
    context: Optional[str],
    date: str,
    discovered_by_account_id: Optional[int] = None,
    files_json: Optional[list] = None,
    available: Optional[bool] = None,
    file_count: Optional[int] = None,
    file_size_total: Optional[int] = None,
) -> bool:
    try:
        date_ts = datetime.fromisoformat(date.replace("Z", "+00:00")) if date else datetime.utcnow()
        probed_at = datetime.utcnow() if files_json is not None else None
        fj_str = _json.dumps(files_json) if files_json is not None else None
        await _exec(
            """INSERT INTO links (group_id, message_id, platform, url, context, date, discovered_by_account_id,
                                  files_json, available, file_count, file_size_total, probed_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)""",
            group_id, message_id, platform, url, context, date_ts, discovered_by_account_id,
            fj_str, available, file_count, file_size_total, probed_at,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


# ── Link probe helpers ────────────────────────────────────────────────────────

_DEAD_PLATFORMS = (
    "Zippyshare",   # closed Mar 2023
    "Uploaded",     # closed 2024
    "Anonfiles",    # closed Aug 2023
    "Bayfiles",     # closed with Anonfiles
)


async def delete_dead_platforms() -> int:
    """Remove links from file hosts that have permanently shut down.
    Idempotent — subsequent runs match zero rows."""
    status = await _exec(
        "DELETE FROM links WHERE platform = ANY($1::text[])",
        list(_DEAD_PLATFORMS),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except Exception:
        return 0


async def reset_mega_probes_for_rescan() -> int:
    """One-shot helper: invalidate `probed_at` on Mega links that don't yet
    carry a decrypted filename so the (now decryption-aware) link prober
    re-fetches them. Idempotent — after the new probe writes real names,
    later runs match zero rows. The rate-limit gate inside link_prober keeps
    Mega's API happy."""
    status = await _exec(
        """UPDATE links
              SET probed_at = NULL
            WHERE platform = 'Mega'
              -- Skip links that already failed decryption permanently.
              AND (probe_error IS NULL OR probe_error NOT LIKE 'mega:%')
              AND (files_json IS NULL
                   OR files_json::text LIKE '%şifreli%'
                   OR files_json::text LIKE '%encrypted%'
                   OR file_count IS NULL
                   OR file_count = 0)"""
    )
    # asyncpg's _exec returns a status string like "UPDATE 42".
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except Exception:
        return 0


async def get_links_due_for_probe(limit: int = 50, stale_days: int = 7) -> List[Dict]:
    """Pick up unprobed links first, then ones stale enough to recheck. Magnet links are excluded — their info is parsed from the URI at insert time."""
    rows = await _q(
        """SELECT id, url, platform
             FROM links
            WHERE (platform IS NULL OR platform != 'Magnet')
              AND (probed_at IS NULL
               OR probed_at < NOW() - INTERVAL '1 day' * $1)
            ORDER BY probed_at NULLS FIRST, id DESC
            LIMIT $2""",
        stale_days, limit,
    )
    return [dict(r) for r in rows]


async def list_magnet_links_needing_enrich(limit: int = 500) -> List[Dict]:
    """Magnet links whose `files_json` only carries the magnet's display name
    (file_count <= 1) — i.e. metadata has not been fetched from the swarm yet.

    Ordered oldest-first so the backfill processes the longest-waiting links
    before recently-discovered ones.
    """
    rows = await _q(
        """SELECT id, url
             FROM links
            WHERE platform = 'Magnet'
              AND (file_count IS NULL OR file_count <= 1)
              AND (probe_error IS NULL OR probe_error NOT LIKE 'magnet-enrich:%')
            ORDER BY id ASC
            LIMIT $1""",
        limit,
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


_SORT_COLS = {"date": "f.date", "name": "f.file_name", "size": "f.file_size", "group": "g.name", "shares": "share_count"}


def _hybrid_order(sort_by: str, sort_dir: str) -> str:
    """Translate the user-facing sort knob (date / name / size / group /
    shares) into an ORDER BY clause for the hybrid (semantic + lexical)
    search result. RRF score becomes a tie-breaker so that for the default
    sort=date you still see the most relevant rows first, but when the
    user clicks "Size" they actually get size ordering — not the previous
    behaviour of silently keeping RRF order."""
    d = "DESC" if (sort_dir or "desc").lower() != "asc" else "ASC"
    cols = {
        "size":   f"fc.file_size {d} NULLS LAST",
        "name":   f"fc.file_name {d} NULLS LAST",
        "group":  f"g.name {d} NULLS LAST",
        "date":   f"fc.date {d}",
        "shares": f"fc.share_count {d}",
    }
    primary = cols.get(sort_by)
    if primary:
        # Explicit sort wins; RRF only breaks ties.
        return f"{primary}, fused.s DESC, fc.date DESC"
    # No usable sort_by → relevance first, then date.
    return "fused.s DESC, fc.date DESC"
_SORT_DIRS = {"asc": "ASC", "desc": "DESC"}

_EXT_GROUPS: Dict[str, List[str]] = {
    "audio":    ["mp3","flac","wav","aac","ogg","m4a","opus","wma","ape","alac","mid","midi"],
    "video":    ["mp4","mkv","avi","mov","wmv","flv","webm","m4v","ts","vob","rm","rmvb","3gp"],
    "image":    ["jpg","jpeg","png","gif","bmp","webp","svg","tiff","tif","heic","ico","raw"],
    "archive":  ["zip","rar","7z","tar","gz","bz2","xz","zst","cab","ace","lzh","lz4","iso"],
    "document": ["pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","txt","epub","rtf","csv","md"],
    "software": ["exe","apk","dmg","deb","rpm","msi","pkg","bin","jar","sh","bat","ps1"],
    "torrent":  ["torrent"],
}


async def _search_files_hybrid(
    *, query: str, mode: str,
    ext: str, ext_group: str,
    group_id: Optional[int], group_ids: Optional[List[int]],
    file_ids: Optional[List[int]],
    date_from: Optional[str], date_to: Optional[str],
    size_min: Optional[int], size_max: Optional[int],
    sort_by: str = "date", sort_dir: str = "desc",
    limit: int, offset: int,
) -> Optional[Tuple[List[Dict], Dict]]:
    """Hybrid (lexical + semantic) search using Reciprocal Rank Fusion.
    Returns None if no embedding can be computed for the query (caller
    should fall back to the lexical path)."""
    import embed as _embed
    qvec = await _embed.embed_query(query)
    if qvec is None:
        return None

    # Build the "non-query" filter clauses, all referencing fc.* (MV alias).
    extra: List[str] = []
    args: List[Any] = []
    idx = 3  # $1=query LIKE, $2=qvec, fillers start at $3
    if ext:
        extra.append(f"LOWER(fc.file_ext) = LOWER(${idx})")
        args.append(ext.lstrip(".")); idx += 1
    elif ext_group:
        exts = _EXT_GROUPS.get(ext_group, [])
        if exts:
            extra.append(f"LOWER(fc.file_ext) = ANY(${idx}::text[])")
            args.append(exts); idx += 1
    if file_ids:
        extra.append(f"fc.id = ANY(${idx}::bigint[])"); args.append(file_ids); idx += 1
    if group_ids:
        extra.append(f"fc.group_id = ANY(${idx}::bigint[])"); args.append(group_ids); idx += 1
    elif group_id is not None:
        extra.append(f"fc.group_id = ${idx}"); args.append(group_id); idx += 1
    if date_from:
        extra.append(f"fc.date >= ${idx}"); args.append(date_from); idx += 1
    if date_to:
        extra.append(f"fc.date <= ${idx}"); args.append(date_to + "T23:59:59"); idx += 1
    if size_min is not None:
        extra.append(f"fc.file_size >= ${idx}"); args.append(size_min); idx += 1
    if size_max is not None:
        extra.append(f"fc.file_size <= ${idx}"); args.append(size_max); idx += 1
    extra_where = (" AND " + " AND ".join(extra)) if extra else ""

    like_pat = f"%{query.strip()}%"
    # $1 = like pattern (used by exact CTE), $2 = query embedding vector,
    # $idx, $idx+1 = limit/offset (added at the end).
    # k=60 in 1/(k+r) is the standard RRF damping constant.
    sql = f"""
        WITH
        exact_hits AS (
          SELECT fc.id,
                 ROW_NUMBER() OVER (ORDER BY fc.date DESC) AS r
          FROM files_canonical fc
          WHERE fc.file_name ILIKE $1 {extra_where}
          LIMIT 300
        ),
        sem_hits AS (
          SELECT fc.id,
                 ROW_NUMBER() OVER (ORDER BY fl2.name_embedding <=> $2::vector) AS r
          FROM files fl2
          JOIN files_canonical fc ON fc.id = fl2.id
          WHERE fl2.name_embedding IS NOT NULL {extra_where}
          ORDER BY fl2.name_embedding <=> $2::vector
          LIMIT 300
        ),
        fused AS (
          SELECT id, SUM(1.0 / (60.0 + r))::float AS s
          FROM (
            SELECT id, r FROM exact_hits
            UNION ALL
            SELECT id, r FROM sem_hits
          ) u
          GROUP BY id
        )
        SELECT fc.id, fc.group_id, fc.message_id, fc.file_name, fc.file_ext,
               fc.mime_type, fc.file_size, fc.date,
               fl.local_path, fl.downloading, fl.download_progress,
               fc.context, fc.appearances,
               fc.share_count, fc.share_count_7d, fc.share_count_30d,
               COALESCE(g.display_name, g.name) AS group_name,
               g.username AS group_username,
               fused.s AS rrf_score
        FROM fused
        JOIN files_canonical fc ON fc.id = fused.id
        JOIN files          fl ON fl.id = fc.id
        JOIN groups         g  ON g.id  = fc.group_id
        ORDER BY {_hybrid_order(sort_by, sort_dir)}
        LIMIT ${idx} OFFSET ${idx+1}
    """
    rows = await _q(sql, like_pat, qvec, *args, limit, offset)
    result = [dict(r) for r in rows]

    # Stats: count of fused candidates (post-filter). For consistency with the
    # lexical path's virtual_total + total_size we still compute these via
    # the existing helper, applying the same `like OR semantic` filter.
    cnt_sql = f"""
        WITH fused AS (
          SELECT id FROM (
            SELECT fc.id FROM files_canonical fc
            WHERE fc.file_name ILIKE $1 {extra_where}
            UNION
            SELECT fc.id FROM files_canonical fc
            JOIN files fl2 ON fl2.id = fc.id AND fl2.name_embedding IS NOT NULL
            {("WHERE" + extra_where.lstrip(" AND")) if extra_where else ""}
            ORDER BY 1 LIMIT 600
          ) u
        )
        SELECT COUNT(*)::bigint AS total,
               COALESCE(SUM(CASE WHEN fc.file_ext='torrent' AND tc.file_count IS NOT NULL
                                 THEN tc.file_count ELSE 1 END), 0)::bigint AS virtual_total,
               COALESCE(SUM(fc.file_size), 0)::bigint AS total_size
        FROM fused
        JOIN files_canonical fc ON fc.id = fused.id
        LEFT JOIN torrent_contents tc ON tc.file_id = fc.id AND tc.error IS NULL
    """
    try:
        srow = await _qrow(cnt_sql, like_pat, qvec, *args)
        stats = {
            "total":         int(srow["total"] or 0)         if srow else 0,
            "virtual_total": int(srow["virtual_total"] or 0) if srow else 0,
            "total_size":    int(srow["total_size"] or 0)    if srow else 0,
        }
    except Exception:
        stats = {"total": len(result), "virtual_total": len(result), "total_size": 0}
    return result, stats


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
    mode: str = "exact",
    search_caption: bool = False,
) -> Tuple[List[Dict], Dict]:
    # Hybrid / semantic dispatch — only valid with dedupe path, a real
    # query string, and pgvector available. Falls through to lexical on
    # any miss so the user never sees an empty grid because of embedding
    # subsystem issues.
    if dedupe and mode in ("semantic", "hybrid") and (query or "").strip() and _VECTOR_AVAILABLE:
        try:
            res = await _search_files_hybrid(
                query=query, mode=mode, ext=ext, ext_group=ext_group,
                group_id=group_id, group_ids=group_ids, file_ids=file_ids,
                date_from=date_from, date_to=date_to,
                size_min=size_min, size_max=size_max,
                sort_by=sort_by, sort_dir=sort_dir,
                limit=limit, offset=offset,
            )
            if res is not None:
                return res
        except Exception as _hybrid_err:
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "hybrid search failed (%s) — falling back to lexical.", _hybrid_err
            )

    col       = _SORT_COLS.get(sort_by, "f.date")
    direction = _SORT_DIRS.get(sort_dir, "DESC")

    conditions: List[str] = []
    args: List[Any] = []
    idx = 1

    for word in query.split():
        or_parts = [f"f.file_name ILIKE ${idx}"]
        if search_caption:
            or_parts.append(f"f.context ILIKE ${idx}")
        or_parts.append(
            f"EXISTS (SELECT 1 FROM torrent_files tf "
            f"        WHERE tf.torrent_id = f.id AND tf.path ILIKE ${idx})"
        )
        conditions.append("(" + " OR ".join(or_parts) + ")")
        args.append(f"%{word}%"); idx += 1
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
                       1 AS share_count, 0 AS share_count_7d, 0 AS share_count_30d,
                       COALESCE(g.display_name, g.name) AS group_name,
                       g.username AS group_username
                FROM files f
                JOIN groups g ON g.id = f.group_id
                {where}
                ORDER BY {col} {direction}
                LIMIT ${idx} OFFSET ${idx+1}""",
            *args,
        )
        row = await _qrow(
            f"""SELECT COUNT(*)::bigint AS total,
                       COALESCE(SUM(f.file_size),0)::bigint AS total_size
                FROM files f JOIN groups g ON g.id = f.group_id {where}""",
            *c_args,
        )
        stats = {
            "total":         int(row["total"] or 0)      if row else 0,
            "virtual_total": int(row["total"] or 0)      if row else 0,
            "total_size":    int(row["total_size"] or 0) if row else 0,
        }
        return [dict(r) for r in rows], stats

    # Dedupe path — served from the files_canonical materialized view.
    # The MV pre-computes the window functions (COUNT/ROW_NUMBER over
    # PARTITION BY (file_name, file_size)) so the runtime query is a plain
    # indexed scan: ms-scale on first page, regardless of total row count.
    # Rewrite the file alias `f.` → `fc.` in the WHERE/ORDER clauses, but
    # use a word-boundary so we don't also rewrite `tf.torrent_id` (the
    # torrent_files alias) into the bogus `tfc.torrent_id`.
    import re as _re_alias
    where_fc = _re_alias.sub(r"\bf\.", "fc.", where)
    col_fc   = _re_alias.sub(r"\bf\.", "fc.", col)
    args += [limit, offset]

    cache_key = _dedupe_rows_key(where_fc, args, col_fc, direction)
    cached_rows = _dedupe_rows_get(cache_key)
    if cached_rows is not None:
        stats = await _files_dedupe_stats_cached(where_fc, c_args)
        return cached_rows, stats

    # Subquery-first pattern: select the page from files_canonical (index scan),
    # then join only the 100 result rows against files/groups (PK lookups).
    # This lets PostgreSQL use idx_fc_date (or other sort indexes) without
    # scanning the full MV just to sort — critical when name_embedding makes
    # each row wide and the default sort has no filter to reduce cardinality.
    col_inner = col_fc                            # fc.date / fc.file_size …
    col_outer = col_fc.replace("fc.", "fc2.")     # fc2.date / fc2.file_size …
    sql = f"""SELECT fc2.id, fc2.group_id, fc2.message_id, fc2.file_name, fc2.file_ext,
                     fc2.mime_type, fc2.file_size, fc2.date,
                     fl.local_path, fl.downloading, fl.download_progress,
                     fc2.context, fc2.appearances,
                     fc2.share_count, fc2.share_count_7d, fc2.share_count_30d,
                     COALESCE(g.display_name, g.name) AS group_name,
                     g.username AS group_username
              FROM (
                  SELECT fc.id, fc.group_id, fc.message_id, fc.file_name, fc.file_ext,
                         fc.mime_type, fc.file_size, fc.date, fc.context, fc.appearances,
                         fc.share_count, fc.share_count_7d, fc.share_count_30d
                  FROM files_canonical fc
                  {where_fc}
                  ORDER BY {col_inner} {direction}
                  LIMIT ${idx} OFFSET ${idx+1}
              ) fc2
              JOIN groups g  ON g.id  = fc2.group_id
              JOIN files  fl ON fl.id = fc2.id
              ORDER BY {col_outer} {direction}"""

    rows = await _q(sql, *args)
    result = [dict(r) for r in rows]
    _dedupe_rows_set(cache_key, result)
    stats = await _files_dedupe_stats_cached(where_fc, c_args)
    return result, stats


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
    when local_path / downloading state changes for many rows. Also
    schedules a throttled background refresh of files_canonical."""
    _DEDUPE_ROWS_CACHE.clear()
    _DEDUPE_COUNT_CACHE.clear()
    _schedule_fc_refresh()


# Materialized view refresh — serialized via lock so concurrent invalidations
# don't pile up REFRESH commands. CONCURRENTLY keeps readers unblocked.
import asyncio as _asyncio
_FC_REFRESH_LOCK = _asyncio.Lock()
_FC_LAST_REFRESH_TS = 0.0
_FC_REFRESH_THROTTLE = 3.0  # min seconds between invalidation-triggered refreshes


async def refresh_files_canonical(force: bool = False) -> None:
    """Refresh the files_canonical materialized view. Idempotent and
    re-entrant via _FC_REFRESH_LOCK. force=True bypasses the throttle."""
    global _FC_LAST_REFRESH_TS
    import time as _t
    now = _t.time()
    if not force and (now - _FC_LAST_REFRESH_TS) < _FC_REFRESH_THROTTLE:
        return
    async with _FC_REFRESH_LOCK:
        # Re-check inside the lock: another waiter may have just refreshed.
        if not force and (_t.time() - _FC_LAST_REFRESH_TS) < _FC_REFRESH_THROTTLE:
            return
        try:
            await _exec("REFRESH MATERIALIZED VIEW CONCURRENTLY files_canonical")
        except Exception:
            # CONCURRENTLY requires the MV to have data. If we somehow lost it
            # (manual TRUNCATE etc.), fall back to a regular refresh.
            try:
                await _exec("REFRESH MATERIALIZED VIEW files_canonical")
            except Exception as e:
                import logging as _lg
                _lg.getLogger(__name__).warning("refresh files_canonical: %s", e)
                return
        # VACUUM removes dead tuples left by CONCURRENTLY (which replaces all rows
        # each run). Without it the table bloats to 22+ GB after ~15 refreshes.
        # ANALYZE follows to give the planner accurate row counts.
        try:
            await _exec("VACUUM ANALYZE files_canonical")
        except Exception:
            try:
                await _exec("ANALYZE files_canonical")
            except Exception:
                pass
        _FC_LAST_REFRESH_TS = _t.time()


def _schedule_fc_refresh() -> None:
    """Fire-and-forget background refresh. Safe to call without an active
    event loop (no-op then). Throttled via refresh_files_canonical()."""
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(refresh_files_canonical())
    except Exception:
        pass


# ── Trend / "Most Shared" feature (option b+c) ────────────────────────────
# Returns the top-N files ranked by `share_count` (number of distinct groups
# that have shared them). For each item, also returns a 7-day per-day
# "share velocity" series so the UI can draw a sparkline and badge fast risers.

async def get_top_shared_files(
    window: str = "all",
    limit: int = 30,
    min_shares: int = 2,
) -> List[Dict]:
    """Top-N most-shared (file_name, file_size) pairs.

    window: 'all' (share_count), '7d' (share_count_7d), '30d' (share_count_30d)
            — determines both the ranking metric AND the floor (min_shares).
    """
    sort_col = {
        "all": "share_count",
        "7d":  "share_count_7d",
        "30d": "share_count_30d",
    }.get(window, "share_count")

    rows = await _q(
        f"""SELECT fc.id, fc.file_name, fc.file_ext, fc.mime_type,
                   fc.file_size, fc.date, fc.local_path,
                   fc.share_count, fc.share_count_7d, fc.share_count_30d,
                   fc.appearances,
                   COALESCE(g.display_name, g.name) AS group_name,
                   g.username AS group_username,
                   fc.group_id, fc.message_id
              FROM files_canonical fc
              LEFT JOIN groups g ON g.id = fc.group_id
              WHERE fc.file_name IS NOT NULL AND fc.file_name <> ''
                AND {sort_col} >= $1
              ORDER BY {sort_col} DESC, fc.date DESC
              LIMIT $2""",
        min_shares, limit,
    )
    items = [dict(r) for r in rows]
    if not items:
        return []

    # Spark series: per-day distinct group count for the last 7 days, keyed
    # by canonical (file_name, file_size). Pass two parallel arrays through
    # unnest() so PostgreSQL infers parameter types correctly (text + bigint)
    # — a literal VALUES list infers both columns as text and the join then
    # fails with "operator does not exist: text = bigint".
    names_arg = [it["file_name"] for it in items]
    sizes_arg = [int(it["file_size"] or 0) for it in items]
    spark_rows = await _q(
        """WITH targets AS (
             SELECT t.file_name, t.file_size
               FROM unnest($1::text[], $2::bigint[]) AS t(file_name, file_size)
           )
           SELECT f.file_name, f.file_size,
                  (date_trunc('day', COALESCE(f.synced_at, f.date)))::date AS d,
                  COUNT(DISTINCT f.group_id)::int AS n
             FROM files f
             JOIN targets t ON t.file_name = f.file_name
                           AND t.file_size = f.file_size
            WHERE COALESCE(f.synced_at, f.date) >= NOW() - INTERVAL '7 days'
            GROUP BY f.file_name, f.file_size, d
            ORDER BY d""",
        names_arg, sizes_arg,
    )
    by_key: Dict[Tuple[str, int], List[Dict]] = {}
    for r in spark_rows:
        k = (r["file_name"], int(r["file_size"] or 0))
        by_key.setdefault(k, []).append({"d": r["d"].isoformat(), "n": int(r["n"])})

    # Top groups currently sharing each file — same unnest pattern.
    top_grp_rows = await _q(
        """WITH targets AS (
             SELECT t.file_name, t.file_size
               FROM unnest($1::text[], $2::bigint[]) AS t(file_name, file_size)
           )
           SELECT f.file_name, f.file_size, g.id AS group_id,
                  COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
             FROM files f
             JOIN groups g ON g.id = f.group_id
             JOIN targets t ON t.file_name = f.file_name
                           AND t.file_size = f.file_size
            GROUP BY f.file_name, f.file_size, g.id, g.display_name, g.name, g.username""",
        names_arg, sizes_arg,
    )
    groups_by_key: Dict[Tuple[str, int], List[Dict]] = {}
    for r in top_grp_rows:
        k = (r["file_name"], int(r["file_size"] or 0))
        groups_by_key.setdefault(k, []).append({
            "id": int(r["group_id"]),
            "name": r["group_name"],
            "username": r["group_username"],
        })

    for it in items:
        k = (it["file_name"], int(it["file_size"] or 0))
        it["spark_7d"] = by_key.get(k, [])
        it["sharing_groups"] = groups_by_key.get(k, [])[:8]
        # Rising = recent share velocity outpaces 30-day average.
        # Using week*4 vs month so equally-active files compare cleanly;
        # the floor (week >= 2) prevents single-share false positives.
        wk  = int(it.get("share_count_7d") or 0)
        mo  = int(it.get("share_count_30d") or 0)
        it["is_rising"] = wk >= 2 and wk * 4 > mo

    return items


def _dedupe_count_key(where: str, c_args: List) -> str:
    import hashlib, json as _j
    payload = where + "|" + _j.dumps([str(a) for a in c_args], default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _files_dedupe_count_cached(where: str, c_args: List) -> int:
    stats = await _files_dedupe_stats_cached(where, c_args)
    return stats["total"]


async def _files_dedupe_stats_cached(where: str, c_args: List) -> Dict:
    """Returns {total, virtual_total, total_size} for the filtered dedupe set.
    - total:         deduped file rows count (one per file_name/file_size pair)
    - virtual_total: same, but each parsed torrent contributes its inner file_count
                     instead of 1 (parallel to total_virtual_files in get_stats)
    - total_size:    sum of fc.file_size across the filtered set (already
                     includes torrent content sizes — save_torrent_tree updates
                     the torrent row's file_size to the inner total)."""
    import time as _t
    key = _dedupe_count_key(where, c_args)
    now = _t.time()
    cached = _DEDUPE_COUNT_CACHE.get(key)
    if cached and now - cached[0] < _DEDUPE_COUNT_TTL:
        return cached[1]
    row = await _qrow(
        f"""SELECT
                COUNT(*)::bigint AS total,
                COALESCE(SUM(CASE WHEN fc.file_ext = 'torrent' AND tc.file_count IS NOT NULL
                                  THEN tc.file_count ELSE 1 END), 0)::bigint AS virtual_total,
                COALESCE(SUM(fc.file_size), 0)::bigint AS total_size
            FROM files_canonical fc
            LEFT JOIN torrent_contents tc
                   ON tc.file_id = fc.id AND tc.error IS NULL
            {where}""",
        *c_args,
    )
    stats = {
        "total":         int(row["total"] or 0)         if row else 0,
        "virtual_total": int(row["virtual_total"] or 0) if row else 0,
        "total_size":    int(row["total_size"] or 0)    if row else 0,
    }
    _DEDUPE_COUNT_CACHE[key] = (now, stats)
    if len(_DEDUPE_COUNT_CACHE) > 64:
        oldest = min(_DEDUPE_COUNT_CACHE, key=lambda k: _DEDUPE_COUNT_CACHE[k][0])
        _DEDUPE_COUNT_CACHE.pop(oldest, None)
    return stats


_LINK_SORT_COLS = {
    "date":     "l.date",
    "url":      "l.url",
    "platform": "l.platform",
    "files":    "l.file_count",
    "size":     "l.file_size_total",
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
    file_name_filter: str = "",
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
    if file_name_filter:
        conditions.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(l.files_json, '[]'::jsonb)) AS f"
            f" WHERE f->>'name' ILIKE ${idx})"
        )
        args.append(f"%{file_name_filter}%"); idx += 1
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
                       l.probe_error,
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
                    l.probe_error,
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


async def search_link_files(query: str, limit: int = 200, offset: int = 0) -> List[Dict]:
    """files_json içinde dosya adına göre link kayıtlarını arar.
    Her eşleşen (link, dosya) çifti için dosya adı, boyut, kaynak link ve
    kanal bilgisi döndürür. Dosyalar tabında Telegram dosyalarıyla birlikte gösterilir."""
    rows = await _q(
        """SELECT
               l.id          AS link_id,
               lf->>'name'   AS file_name,
               COALESCE((lf->>'size')::bigint, 0) AS file_size,
               l.platform,
               l.url         AS link_url,
               l.group_id,
               l.date,
               COALESCE(g.display_name, g.name) AS group_name,
               g.username    AS group_username
           FROM links l
           JOIN groups g ON g.id = l.group_id,
           jsonb_array_elements(COALESCE(l.files_json, '[]'::jsonb)) AS lf
           WHERE lf->>'name' ILIKE $1
             AND (l.available IS NULL OR l.available = TRUE)
           ORDER BY l.date DESC NULLS LAST
           LIMIT $2 OFFSET $3""",
        f"%{query}%", limit, offset,
    )
    return [dict(r) for r in rows]


async def get_file_by_id(file_id: int) -> Optional[Dict]:
    # Explicit column list — never expose `name_embedding` (pgvector 384-dim
    # float array) to the JSON encoder. FastAPI's jsonable_encoder calls
    # `dict(obj)` / `vars(obj)` on it and fails, returning 500 to every poller
    # (download progress, deep-scan status, etc.). Also avoids the per-row
    # kilobyte bandwidth hit on every poll.
    row = await _qrow(
        """SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
                  f.mime_type, f.file_size, f.date, f.local_path,
                  f.downloading, f.download_progress, f.context,
                  f.downloaded_at, f.synced_at, f.discovered_by_account_id,
                  COALESCE(g.display_name, g.name) AS group_name,
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


async def list_downloading_files() -> List[Dict]:
    rows = await _q(
        """SELECT f.id, f.group_id, f.message_id, f.file_name, f.file_ext,
                  f.mime_type, f.file_size, f.date, f.download_progress,
                  COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
           FROM files f
           JOIN groups g ON g.id = f.group_id
           WHERE f.downloading = TRUE AND f.local_path IS NULL
           ORDER BY f.id"""
    )
    return [dict(r) for r in rows]


async def reset_stale_downloads() -> int:
    """Clear downloading flag for files left in-progress after a dirty shutdown."""
    result = await _exec(
        "UPDATE files SET downloading=FALSE, download_progress=0.0 WHERE downloading=TRUE AND local_path IS NULL"
    )
    # asyncpg returns 'UPDATE N' as a string
    try:
        count = int(str(result).split()[-1])
    except Exception:
        count = 0
    return count


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
    # Scalar queries that don't need torrent expansion
    downloaded       = await _qval("SELECT COUNT(*) FROM files WHERE local_path IS NOT NULL")
    total_groups     = await _qval("SELECT COUNT(*) FROM groups")
    total_links      = await _qval("SELECT COUNT(*) FROM links")
    max_file_size    = await _qval("SELECT COALESCE(MAX(file_size),0) FROM files")
    excluded_grps    = await _qval("SELECT COUNT(*) FROM groups WHERE excluded=TRUE")
    downloaded_size  = await _qval("SELECT COALESCE(SUM(file_size),0) FROM files WHERE local_path IS NOT NULL")

    # Single pass: expand parsed torrents (file_count) into virtual file counts.
    # file_size for parsed torrents is already set to content total_size by save_torrent_tree(),
    # so all SUM(file_size) values are correct without extra joins.
    row = await _qrow(
        """WITH parsed AS (
               SELECT file_id, file_count
               FROM torrent_contents
               WHERE error IS NULL
           ),
           fe AS (
               SELECT f.file_ext, f.synced_at, f.file_size,
                      p.file_count AS tc_count
               FROM files f
               LEFT JOIN parsed p ON p.file_id = f.id
           )
           SELECT
             -- raw row counts (one per Telegram message)
             COUNT(*)::bigint AS total_files,
             COUNT(*) FILTER (WHERE synced_at >= NOW()-INTERVAL '24 hours')::bigint AS real_24h,
             COUNT(*) FILTER (WHERE synced_at >= NOW()-INTERVAL '7 days')::bigint  AS real_7d,
             -- virtual counts (each parsed torrent → its file_count instead of 1)
             SUM(CASE WHEN file_ext='torrent' AND tc_count IS NOT NULL
                      THEN tc_count ELSE 1 END)::bigint AS virtual_all,
             SUM(CASE WHEN synced_at >= NOW()-INTERVAL '24 hours'
                      THEN CASE WHEN file_ext='torrent' AND tc_count IS NOT NULL
                                THEN tc_count ELSE 1 END
                      ELSE 0 END)::bigint AS cnt24,
             SUM(CASE WHEN synced_at >= NOW()-INTERVAL '7 days'
                      THEN CASE WHEN file_ext='torrent' AND tc_count IS NOT NULL
                                THEN tc_count ELSE 1 END
                      ELSE 0 END)::bigint AS cnt7,
             -- sizes (already correct: file_size = content total for parsed torrents)
             COALESCE(SUM(file_size),0)::bigint AS total_size,
             COALESCE(SUM(file_size) FILTER (WHERE synced_at >= NOW()-INTERVAL '24 hours'),0)::bigint AS sz24,
             COALESCE(SUM(file_size) FILTER (WHERE synced_at >= NOW()-INTERVAL '7 days'),0)::bigint AS sz7,
             -- torrent breakdown for the UI note
             COUNT(*) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL)::bigint AS parsed_count,
             COALESCE(SUM(tc_count) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL),0)::bigint AS content_files,
             COALESCE(SUM(tc_count) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL
                                             AND synced_at >= NOW()-INTERVAL '24 hours'),0)::bigint AS content_24h,
             COALESCE(SUM(tc_count) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL
                                             AND synced_at >= NOW()-INTERVAL '7 days'),0)::bigint  AS content_7d,
             COUNT(*) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL
                              AND synced_at >= NOW()-INTERVAL '24 hours')::bigint AS parsed_24h,
             COUNT(*) FILTER (WHERE file_ext='torrent' AND tc_count IS NOT NULL
                              AND synced_at >= NOW()-INTERVAL '7 days')::bigint  AS parsed_7d
           FROM fe"""
    )
    mrow = await _qrow(
        """SELECT COALESCE(SUM(file_count), 0)::bigint     AS magnet_count,
                  COALESCE(SUM(file_size_total), 0)::bigint AS magnet_size
           FROM links
           WHERE platform = 'Magnet' AND file_count > 0 AND available IS NOT FALSE"""
    )
    # Deduped (unique-file) counts straight from the canonical MV — matches
    # the figures the Files-tab pill computes from /api/files, so the bottom
    # status bar can show the same "library size" instead of the raw cross-
    # channel duplicate count.
    drow = await _qrow(
        """SELECT
              COUNT(*)::bigint                                  AS unique_files,
              COALESCE(SUM(fc.file_size), 0)::bigint            AS unique_total_size,
              COALESCE(SUM(CASE WHEN fc.file_ext = 'torrent' AND tc.file_count IS NOT NULL
                                THEN tc.file_count ELSE 1 END), 0)::bigint AS unique_virtual_files
           FROM files_canonical fc
           LEFT JOIN torrent_contents tc
                  ON tc.file_id = fc.id AND tc.error IS NULL"""
    )
    mr = mrow or {}
    r  = row  or {}
    dr = drow or {}
    return {
        "total_files":              int(r.get("total_files")    or 0),
        "total_virtual_files":      int(r.get("virtual_all")    or 0),
        "torrent_parsed_count":     int(r.get("parsed_count")   or 0),
        "torrent_content_files":    int(r.get("content_files")  or 0),
        "torrent_content_24h":      int(r.get("content_24h")    or 0),
        "torrent_parsed_24h":       int(r.get("parsed_24h")     or 0),
        "torrent_content_7d":       int(r.get("content_7d")     or 0),
        "torrent_parsed_7d":        int(r.get("parsed_7d")      or 0),
        "magnet_file_count":        int(mr.get("magnet_count")  or 0),
        "magnet_file_size":         int(mr.get("magnet_size")   or 0),
        "downloaded":               downloaded or 0,
        "total_groups":             total_groups or 0,
        "total_links":              total_links or 0,
        "max_file_size":            max_file_size or 0,
        "excluded_groups":          excluded_grps or 0,
        "total_size":               int(r.get("total_size")     or 0),
        "downloaded_size":          int(downloaded_size or 0),
        "recent_24h":               int(r.get("real_24h")       or 0),
        "recent_24h_size":          int(r.get("sz24")           or 0),
        "recent_7d":                int(r.get("real_7d")        or 0),
        "recent_7d_size":           int(r.get("sz7")            or 0),
        "recent_24h_virtual":       int(r.get("cnt24")          or 0),
        "recent_7d_virtual":        int(r.get("cnt7")           or 0),
        # Deduped (unique-file) counts — see drow above. The Files-tab pill
        # uses these via /api/files; exposing them here lets the status bar
        # match the same library-size narrative.
        "unique_files":             int(dr.get("unique_files")         or 0),
        "unique_virtual_files":     int(dr.get("unique_virtual_files") or 0),
        "unique_total_size":        int(dr.get("unique_total_size")    or 0),
    }


async def export_files_cursor():
    """Async generator yielding the full library as rows
    (file_name, file_size, username, group_name, source_type).
    Server-side cursor — no in-memory buffering of the full table.

    Three sources are concatenated so the export row count matches the
    figure shown in the status bar's "Tümü" pill:
      • telegram      — Telegram message attachments (parsed torrents skipped,
                        their inner files come from torrent_inner instead so we
                        don't double-count the torrent's content size).
      • torrent_inner — every file inside a parsed .torrent (one row per
                        torrent_files entry; the .torrent message itself is
                        suppressed in the telegram pass).
      • magnet_inner  — every file listed in a magnet's `links.files_json`
                        (populated by aria2c+DHT during magnet backfill).
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            # 1. Telegram message attachments, excluding parsed-torrent rows
            #    so we don't ship both a torrent + its inner files (their
            #    file_size sums would overlap).
            async for row in conn.cursor(
                """SELECT f.file_name, f.file_size,
                          g.username, g.name AS group_name,
                          'telegram'::text AS source_type
                     FROM files f
                     JOIN groups g ON g.id = f.group_id
                LEFT JOIN torrent_contents tc
                       ON tc.file_id = f.id AND tc.error IS NULL
                    WHERE NOT (f.file_ext = 'torrent' AND tc.file_id IS NOT NULL)
                    ORDER BY g.name, f.file_name"""
            ):
                yield row
            # 2. Files inside parsed torrents
            async for row in conn.cursor(
                """SELECT tf.path AS file_name,
                          tf.size AS file_size,
                          g.username, g.name AS group_name,
                          'torrent_inner'::text AS source_type
                     FROM torrent_files tf
                     JOIN files f  ON f.id = tf.torrent_id
                     JOIN groups g ON g.id = f.group_id
                    ORDER BY g.name, tf.path"""
            ):
                yield row
            # 3. Files announced inside magnet `files_json` payloads (set by
            #    the magnet-backfill enrich pass). Empty payloads contribute
            #    no rows; the WHERE filters them out at the cursor level.
            async for row in conn.cursor(
                """SELECT COALESCE(entry->>'name', '?')::text AS file_name,
                          COALESCE((entry->>'size')::bigint, 0) AS file_size,
                          g.username, g.name AS group_name,
                          'magnet_inner'::text AS source_type
                     FROM links l
                     JOIN groups g ON g.id = l.group_id
                CROSS JOIN LATERAL jsonb_array_elements(l.files_json) AS entry
                    WHERE l.platform = 'Magnet'
                      AND l.files_json IS NOT NULL
                      AND jsonb_typeof(l.files_json) = 'array'
                      AND jsonb_array_length(l.files_json) > 0
                    ORDER BY g.name"""
            ):
                yield row


# ── Watch terms & notifications ───────────────────────────────────────────────

async def list_watches() -> List[Dict]:
    rows = await _q(
        """SELECT w.id, w.keywords, w.min_size_bytes, w.created_at,
                  w.baseline_file_id, w.last_checked_file_id,
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


async def create_watch(keywords: str, min_size_bytes: int = 0) -> int:
    max_id = await _qval("SELECT COALESCE(MAX(id), 0) FROM files") or 0
    row = await _qrow(
        """INSERT INTO watch_terms (keywords, min_size_bytes, baseline_file_id, last_checked_file_id)
           VALUES ($1, $2, $3, $3) RETURNING id""",
        keywords, int(min_size_bytes), max_id,
    )
    return row["id"]


async def delete_watch(watch_id: int):
    await _exec("DELETE FROM watch_terms WHERE id = $1", watch_id)


async def check_watches() -> Tuple[int, List[Dict]]:
    """For each watch term, find new matching files since last check and accumulate
    them into the active notification (creating one if none exists). Returns
    (total_new_matches, per_watch_payload) where the payload describes the new
    rows so the sync layer can push them to the user's Saved Messages."""
    watches = await _q("SELECT id, keywords, min_size_bytes, last_checked_file_id FROM watch_terms")
    if not watches:
        return 0, []

    cur_max = await _qval("SELECT COALESCE(MAX(id), 0) FROM files") or 0
    total_new = 0
    per_watch: List[Dict] = []

    for w in watches:
        keywords = (w["keywords"] or "").strip()
        if not keywords:
            continue
        terms = [t.strip() for t in keywords.replace(",", " ").split() if t.strip()]
        if not terms:
            continue

        # Watch semantics: ALL keywords must appear in the FILE NAME (AND).
        # Each term contributes one ILIKE condition over file_name only.
        # Optional: minimum file_size condition.
        conds: List[str] = []
        args: List = [w["last_checked_file_id"]]
        idx = 2
        for t in terms:
            conds.append(f"file_name ILIKE ${idx}")
            args.append(f"%{t}%")
            idx += 1

        min_sz = int(w.get("min_size_bytes") or 0)
        size_clause = ""
        if min_sz > 0:
            size_clause = f" AND file_size >= ${idx}"
            args.append(min_sz)

        sql = (
            "SELECT id FROM files "
            f"WHERE id > $1 AND ({' AND '.join(conds)}){size_clause} "
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
            per_watch.append({
                "watch_id": int(w["id"]),
                "keywords": keywords,
                "file_ids": new_ids,
            })

        await _exec(
            "UPDATE watch_terms SET last_checked_file_id = $1 WHERE id = $2",
            cur_max, w["id"],
        )

    return total_new, per_watch


async def get_files_for_notification(file_ids: List[int]) -> List[Dict]:
    """Resolve a list of file ids to the fields a notification message needs:
    file name, file size, group name + username (for the t.me/ link), Telegram
    message id."""
    if not file_ids:
        return []
    rows = await _q(
        """SELECT f.id, f.file_name, f.file_size, f.message_id, f.group_id,
                  COALESCE(g.display_name, g.name) AS group_name,
                  g.username AS group_username
             FROM files f
             JOIN groups g ON g.id = f.group_id
            WHERE f.id = ANY($1::bigint[])
         ORDER BY f.id DESC""",
        list(file_ids),
    )
    return [dict(r) for r in rows]


async def get_notify_settings() -> Dict:
    row = await _qrow("SELECT * FROM notify_settings WHERE id = 1")
    if not row:
        await _exec("INSERT INTO notify_settings (id) VALUES (1) ON CONFLICT DO NOTHING")
        row = await _qrow("SELECT * FROM notify_settings WHERE id = 1")
    return dict(row) if row else {"id": 1, "tg_push_enabled": False, "last_push_at": None}


async def set_notify_settings(*, tg_push_enabled: Optional[bool] = None,
                               last_push_at: Optional[datetime] = None):
    parts: List[str] = []
    args: List[Any] = []
    idx = 1
    if tg_push_enabled is not None:
        parts.append(f"tg_push_enabled = ${idx}"); args.append(bool(tg_push_enabled)); idx += 1
    if last_push_at is not None:
        parts.append(f"last_push_at = ${idx}"); args.append(last_push_at); idx += 1
    if not parts:
        return
    await _exec("INSERT INTO notify_settings (id) VALUES (1) ON CONFLICT DO NOTHING")
    await _exec(f"UPDATE notify_settings SET {', '.join(parts)} WHERE id = 1", *args)


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


async def prune_account_groups(account_id: int, keep_gids: set) -> int:
    """Remove account_groups rows for groups that are no longer in the
    account's Telegram dialogs (i.e. the user left them since last sync).
    Returns the number of rows deleted."""
    if not keep_gids:
        return 0
    tag = await _exec(
        "DELETE FROM account_groups WHERE account_id = $1 AND NOT (group_id = ANY($2::bigint[]))",
        account_id, list(keep_gids),
    )
    try:
        return int(tag.split()[-1])
    except Exception:
        return 0


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
                  COALESCE(SUM(f.file_size) FILTER (WHERE f.discovered_by_account_id = $1), 0) AS total_size,
                  COALESCE(hc.score, 0) AS hunter_score
           FROM account_groups ag
           JOIN groups g ON g.id = ag.group_id
           LEFT JOIN files f ON f.group_id = g.id
           LEFT JOIN hunter_candidates hc ON LOWER(hc.username) = LOWER(g.username)
           WHERE ag.account_id = $1
           GROUP BY g.id, ag.excluded, ag.hidden, ag.display_name, ag.last_synced_at, hc.score
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
    "anthropic_api_key": "",
    "tg_temp_join_enabled": True,
    "skip_old_channels": True,
    # Magnet hunt — discovers magnet URIs via search engine dorks. Now part
    # of the main pipeline as a step between Stage 2 (web seed harvest) and
    # Stage 3 (Telegram enrichment). Disable to skip it without disabling
    # the rest of the run.
    "magnethunt_enabled": True,
    # Magnet backfill — re-walks group history for magnet URIs the live handler
    # may have missed, then enriches magnets that still lack a file list via
    # aria2c+DHT. Used to live in Settings → "Geçmiş Veri Tarama"; now a
    # pipeline stage between Magnet Hunt and Stage 3 enrichment.
    "magnet_backfill_enabled": True,
    "ui_language": "tr",
    "similar_expand_enabled": True,
    "similar_expand_max_per_seed": 10,
    "similar_expand_max_seeds": 100,
    "last_floodwait_until": None,
    "last_floodwait_scope": None,
    "last_floodwait_seconds": None,
    # Auto-learned keyword pool, comma-separated. Grown by Stage 3 after
    # each successful enrichment; merged into the Stage 2 keyword list on
    # the next run.
    "learned_keywords": "",
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


async def check_hunter_candidates_bulk(usernames: list) -> dict:
    """Return {lowercase_username: 'blacklisted'|'joined'|'queued'} for
    usernames that already exist in hunter_blacklist or hunter_candidates.
    Channels absent from both tables are not in the result (→ truly new).
    """
    if not usernames:
        return {}
    lower = [u.lower() for u in usernames]
    result: dict = {}
    # hunter_blacklist covers auto-blacklisted channels not yet in candidates.
    rows = await _q(
        "SELECT username FROM hunter_blacklist WHERE username = ANY($1::text[])", lower
    )
    for r in rows:
        result[r["username"]] = "blacklisted"
    remaining = [u for u in lower if u not in result]
    if remaining:
        rows = await _q(
            "SELECT username, COALESCE(status, 'discovered') AS status "
            "FROM hunter_candidates WHERE username = ANY($1::text[])",
            remaining,
        )
        for r in rows:
            st = r["status"]
            if st == "blacklisted":
                result[r["username"]] = "blacklisted"
            elif st == "joined":
                result[r["username"]] = "joined"
            else:
                result[r["username"]] = "queued"
    return result


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


async def sync_hunter_joined_from_groups() -> int:
    # Bring candidates that the user joined outside the hunter flow (manual
    # Telegram join, account import, etc.) in sync with reality so they stop
    # cluttering the review queue.
    rows = await _q(
        """UPDATE hunter_candidates c
              SET status = 'joined',
                  decided_at = COALESCE(c.decided_at, NOW())
             FROM groups g
             JOIN account_groups ag ON ag.group_id = g.id
            WHERE LOWER(g.username) = LOWER(c.username)
              AND (c.status IS NULL OR c.status NOT IN ('joined','rejected','blacklisted'))
            RETURNING c.id"""
    )
    return len(rows)


async def list_hunter_candidates(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "score",
    sort_dir: str = "desc",
) -> Tuple[List[Dict], int]:
    # Reconcile externally-joined channels before reading so the review queue
    # never shows rows the user has already added through another path.
    try:
        await sync_hunter_joined_from_groups()
    except Exception as e:
        import logging as _lg
        _lg.getLogger("database").warning(
            "sync_hunter_joined_from_groups failed: %s", e
        )
    _d = "ASC" if sort_dir.lower() == "asc" else "DESC"
    _SORT_MAP = {
        "score":           f"score {_d} NULLS LAST",
        "username":        f"username {_d}",
        "members":         f"members {_d} NULLS LAST",
        "estimated_files": f"estimated_files {_d} NULLS LAST",
        "last_message_at": f"last_message_at {_d} NULLS LAST",
        "discovered_at":   f"discovered_at {_d} NULLS LAST",
        "status":          f"status {_d}",
        "type_video":      f"(file_type_breakdown->>'video')::int {_d} NULLS LAST",
        "type_audio":      f"(file_type_breakdown->>'audio')::int {_d} NULLS LAST",
        "type_image":      f"(file_type_breakdown->>'image')::int {_d} NULLS LAST",
        "type_archive":    f"(file_type_breakdown->>'archive')::int {_d} NULLS LAST",
        "type_document":   f"(file_type_breakdown->>'document')::int {_d} NULLS LAST",
        "type_software":   f"(file_type_breakdown->>'software')::int {_d} NULLS LAST",
        "type_other":      f"(file_type_breakdown->>'other')::int {_d} NULLS LAST",
    }
    sort_col = _SORT_MAP.get(sort_by, f"score DESC NULLS LAST")
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


async def get_hunter_candidate_by_username(username: str) -> Optional[Dict]:
    row = await _qrow(
        """SELECT c.*, (SELECT array_agg(DISTINCT s.source ORDER BY s.source)
                        FROM hunter_sources s WHERE s.candidate_id = c.id) AS sources
           FROM hunter_candidates c WHERE LOWER(c.username) = LOWER($1)""",
        username,
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
                                 file_group: Optional[str], date,
                                 is_named: bool = True) -> bool:
    try:
        await _exec(
            """INSERT INTO hunter_candidate_files
               (candidate_id, message_id, file_name, file_ext, file_size, file_group, date, is_named)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (candidate_id, message_id) DO NOTHING""",
            candidate_id, message_id, file_name, file_ext, file_size, file_group, date, is_named,
        )
        return True
    except Exception:
        return False


async def get_candidate_file(candidate_id: int, message_id: int) -> Optional[Dict]:
    row = await _qrow(
        """SELECT candidate_id, message_id, file_name, file_ext, file_size,
                  file_group, date, local_path, downloaded_at
           FROM hunter_candidate_files
           WHERE candidate_id = $1 AND message_id = $2""",
        candidate_id, message_id,
    )
    return dict(row) if row else None


async def set_candidate_file_local_path(candidate_id: int, message_id: int, path: str) -> None:
    await _exec(
        """UPDATE hunter_candidate_files
           SET local_path = $3, downloaded_at = NOW()
           WHERE candidate_id = $1 AND message_id = $2""",
        candidate_id, message_id, path,
    )


async def clear_candidate_file_local_path(candidate_id: int, message_id: int) -> None:
    await _exec(
        """UPDATE hunter_candidate_files
           SET local_path = NULL, downloaded_at = NULL
           WHERE candidate_id = $1 AND message_id = $2""",
        candidate_id, message_id,
    )


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
        f"""SELECT message_id, file_name, file_ext, file_size, file_group, date,
                   local_path, downloaded_at, is_named
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
                  MIN(date) AS first_date,
                  COUNT(*) FILTER (WHERE is_named) AS named_count,
                  COUNT(*) FILTER (WHERE NOT is_named) AS ephemeral_count,
                  COALESCE(SUM(file_size) FILTER (WHERE is_named), 0) AS named_size,
                  COALESCE(SUM(file_size) FILTER (WHERE NOT is_named), 0) AS ephemeral_size
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


# ── Channel files — serves the unified detail lightbox from the Channels tab ──

_EXT_TO_GROUP: Dict[str, str] = {e: g for g, exts in _EXT_GROUPS.items() for e in exts}


async def list_channel_files(
    group_id: int,
    q: str = "",
    ext: str = "",
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 500,
    offset: int = 0,
) -> Tuple[List[Dict], int]:
    col_map = {"date": "f.date", "name": "f.file_name", "size": "f.file_size", "ext": "f.file_ext"}
    col = col_map.get(sort_by, "f.date")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    where_parts = ["f.group_id = $1"]
    args: List[Any] = [group_id]
    idx = 2
    if q:
        where_parts.append(f"f.file_name ILIKE ${idx}"); args.append(f"%{q}%"); idx += 1
    if ext:
        where_parts.append(f"LOWER(f.file_ext) = LOWER(${idx})"); args.append(ext.lstrip(".")); idx += 1
    wsql = " AND ".join(where_parts)
    total = await _qval(f"SELECT COUNT(*) FROM files f WHERE {wsql}", *args) or 0
    args.extend([limit, offset])
    rows = await _q(
        f"""SELECT f.id AS message_id, f.file_name, f.file_ext, f.file_size, f.date, f.local_path,
                   CASE
                     WHEN f.file_name IS NULL OR f.file_name = '' THEN FALSE
                     WHEN f.file_name ~ '^(video|audio|image|archive|document|app)_[0-9]' THEN FALSE
                     ELSE TRUE
                   END AS is_named
            FROM files f
            WHERE {wsql}
            ORDER BY {col} {direction} NULLS LAST
            LIMIT ${idx} OFFSET ${idx+1}""",
        *args,
    )
    result = []
    for r in rows:
        d = dict(r)
        fext = (d.get("file_ext") or "").lower().lstrip(".")
        d["file_group"] = _EXT_TO_GROUP.get(fext, "other")
        result.append(d)
    return result, int(total)


async def channel_file_summary(group_id: int) -> Dict:
    row = await _qrow(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(file_size), 0) AS total_size,
                  COUNT(*) FILTER (WHERE file_name IS NOT NULL AND file_name != ''
                    AND NOT (file_name ~ '^(video|audio|image|archive|document|app)_[0-9]')) AS named_count,
                  COUNT(*) FILTER (WHERE file_name IS NULL OR file_name = ''
                    OR file_name ~ '^(video|audio|image|archive|document|app)_[0-9]') AS ephemeral_count,
                  COALESCE(SUM(file_size) FILTER (WHERE file_name IS NOT NULL AND file_name != ''
                    AND NOT (file_name ~ '^(video|audio|image|archive|document|app)_[0-9]')), 0) AS named_size,
                  COALESCE(SUM(file_size) FILTER (WHERE file_name IS NULL OR file_name = ''
                    OR file_name ~ '^(video|audio|image|archive|document|app)_[0-9]'), 0) AS ephemeral_size
           FROM files WHERE group_id = $1""",
        group_id,
    )
    return dict(row) if row else {}


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
    """Return groups not yet reported, with ≥ 100 files, including per-type
    file counts. Channels already in telemetry_sent_groups are excluded."""
    audio_l = _ext_list_sql("audio")
    video_l = _ext_list_sql("video")
    image_l = _ext_list_sql("image")
    archv_l = _ext_list_sql("archive")
    docu_l  = _ext_list_sql("document")
    soft_l  = _ext_list_sql("software")
    torr_l  = _ext_list_sql("torrent")
    # Build a single flat list of all known extensions for the NOT IN clause.
    all_known = _ext_list_sql_combined("audio", "video", "image", "archive",
                                       "document", "software", "torrent")
    rows = await _q(
        f"""SELECT g.id, g.username, g.is_channel, g.member_count,
                  COUNT(f.id)                                                          AS file_count,
                  COALESCE(SUM(f.file_size), 0)                                        AS total_size,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {audio_l})            AS type_audio,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {video_l})            AS type_video,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {image_l})            AS type_image,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {archv_l})            AS type_archive,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {docu_l})             AS type_document,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {soft_l})             AS type_software,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) IN {torr_l})             AS type_torrent,
                  COUNT(f.id) FILTER (WHERE LOWER(f.file_ext) NOT IN {all_known})      AS type_other
           FROM groups g
           LEFT JOIN files f ON f.group_id = g.id
           WHERE g.id NOT IN (SELECT group_id FROM telemetry_sent_groups)
           GROUP BY g.id
           HAVING COUNT(f.id) >= 100
           ORDER BY g.id"""
    )
    return [dict(r) for r in rows]


async def mark_groups_sent(group_ids: List[int]) -> None:
    """Record group IDs as reported so they are excluded from future payloads."""
    if not group_ids:
        return
    await _exec(
        "INSERT INTO telemetry_sent_groups (group_id) "
        "SELECT unnest($1::bigint[]) ON CONFLICT DO NOTHING",
        group_ids,
    )


_TELEMETRY_FILE_BATCH = 100_000


async def get_files_for_telemetry() -> tuple[list[dict], bool]:
    """Return up to _TELEMETRY_FILE_BATCH unsent files and whether more remain.

    Uses LIMIT+1 trick to detect remaining rows without a separate COUNT query.
    Filenames are truncated at 200 chars to cap row size.
    Returns (rows, has_more).
    """
    rows = await _q(
        """SELECT f.id, LEFT(f.file_name, 200) AS file_name,
                  f.file_size, g.username
           FROM files f
           JOIN groups g ON g.id = f.group_id
           WHERE f.id NOT IN (SELECT file_id FROM telemetry_sent_files)
             AND f.file_name IS NOT NULL AND f.file_name <> ''
           ORDER BY f.id
           LIMIT $1""",
        _TELEMETRY_FILE_BATCH + 1,
    )
    if not rows:
        return [], False
    has_more = len(rows) > _TELEMETRY_FILE_BATCH
    return [dict(r) for r in rows[:_TELEMETRY_FILE_BATCH]], has_more


async def mark_files_sent(file_ids: list[int]) -> None:
    """Record file IDs as reported so they are excluded from future payloads."""
    if not file_ids:
        return
    await _exec(
        "INSERT INTO telemetry_sent_files (file_id) "
        "SELECT unnest($1::bigint[]) ON CONFLICT DO NOTHING",
        file_ids,
    )


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


# ── Transfer Destinations ─────────────────────────────────────────────────────

async def list_transfer_destinations() -> List[Dict]:
    rows = await _q("SELECT * FROM transfer_destinations ORDER BY id")
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("config"), str):
            d["config"] = _json.loads(d["config"])
        result.append(d)
    return result


async def get_transfer_destination(dest_id: int) -> Optional[Dict]:
    row = await _qrow("SELECT * FROM transfer_destinations WHERE id=$1", dest_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("config"), str):
        d["config"] = _json.loads(d["config"])
    return d


async def create_transfer_destination(name: str, type_: str, config: dict, enabled: bool = True) -> Dict:
    row = await _qrow(
        """INSERT INTO transfer_destinations (name, type, config, enabled)
           VALUES ($1, $2, $3::jsonb, $4)
           RETURNING *""",
        name, type_, _json.dumps(config), enabled,
    )
    d = dict(row)
    if isinstance(d.get("config"), str):
        d["config"] = _json.loads(d["config"])
    return d


async def update_transfer_destination(dest_id: int, name: str, type_: str, config: dict, enabled: bool) -> Optional[Dict]:
    row = await _qrow(
        """UPDATE transfer_destinations
           SET name=$1, type=$2, config=$3::jsonb, enabled=$4
           WHERE id=$5
           RETURNING *""",
        name, type_, _json.dumps(config), enabled, dest_id,
    )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("config"), str):
        d["config"] = _json.loads(d["config"])
    return d


async def delete_transfer_destination(dest_id: int) -> bool:
    result = await _exec("DELETE FROM transfer_destinations WHERE id=$1", dest_id)
    return "DELETE 1" in str(result)


# ── Bandwidth Scheduling ───────────────────────────────────────────────────────

async def get_bandwidth_settings() -> Dict:
    row = await _qrow("SELECT enabled, min_size_mb FROM bandwidth_settings WHERE id = 1")
    if not row:
        return {"enabled": False, "min_size_mb": 0}
    return dict(row)


async def set_bandwidth_settings(enabled: bool, min_size_mb: int) -> None:
    await _exec(
        "UPDATE bandwidth_settings SET enabled=$1, min_size_mb=$2 WHERE id=1",
        enabled, min_size_mb,
    )


async def list_bandwidth_schedules() -> List[Dict]:
    rows = await _q("SELECT * FROM bandwidth_schedules ORDER BY created_at")
    return [dict(r) for r in rows]


async def create_bandwidth_schedule(data: Dict) -> Dict:
    row = await _qrow(
        """INSERT INTO bandwidth_schedules
               (name, enabled, rule_type, days, start_time, end_time, specific_date)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        data["name"],
        bool(data.get("enabled", True)),
        data.get("rule_type", "weekly"),
        data.get("days") or [],
        data.get("start_time", "02:00"),
        data.get("end_time", "06:00"),
        data.get("specific_date") or None,
    )
    return dict(row)


async def update_bandwidth_schedule(id: int, data: Dict) -> Optional[Dict]:
    row = await _qrow(
        """UPDATE bandwidth_schedules
           SET name=$1, enabled=$2, rule_type=$3, days=$4,
               start_time=$5, end_time=$6, specific_date=$7
           WHERE id=$8 RETURNING *""",
        data["name"],
        bool(data.get("enabled", True)),
        data.get("rule_type", "weekly"),
        data.get("days") or [],
        data.get("start_time", "02:00"),
        data.get("end_time", "06:00"),
        data.get("specific_date") or None,
        id,
    )
    return dict(row) if row else None


async def delete_bandwidth_schedule(id: int) -> None:
    await _exec("DELETE FROM bandwidth_schedules WHERE id=$1", id)


async def list_scheduled_downloads() -> List[Dict]:
    rows = await _q(
        """SELECT sd.file_id, sd.destination_ids, sd.queued_at, sd.scheduled_at,
                  f.file_name, f.file_size, f.file_ext,
                  COALESCE(g.display_name, g.name) AS group_name
           FROM scheduled_downloads sd
           JOIN files f ON f.id = sd.file_id
           JOIN groups g ON g.id = f.group_id
           ORDER BY sd.queued_at"""
    )
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("destination_ids"), str):
            import json as _j
            try:
                d["destination_ids"] = _j.loads(d["destination_ids"])
            except Exception:
                d["destination_ids"] = []
        result.append(d)
    return result


async def add_scheduled_download(file_id: int, destination_ids: List[int], scheduled_at=None) -> None:
    import json as _j
    await _exec(
        """INSERT INTO scheduled_downloads (file_id, destination_ids, scheduled_at)
           VALUES ($1, $2::jsonb, $3)
           ON CONFLICT (file_id) DO UPDATE SET destination_ids=$2::jsonb, scheduled_at=$3, queued_at=NOW()""",
        file_id,
        _j.dumps(destination_ids),
        scheduled_at,
    )


async def remove_scheduled_download(file_id: int) -> None:
    await _exec("DELETE FROM scheduled_downloads WHERE file_id=$1", file_id)


async def count_scheduled_downloads() -> int:
    return (await _qval("SELECT COUNT(*) FROM scheduled_downloads")) or 0


# ── Torrent contents ───────────────────────────────────────────────────────────

async def get_torrent_tree(file_id: int) -> Optional[Dict]:
    row = await _qrow(
        "SELECT file_id, torrent_name, total_size, file_count, tree, parsed_at, error "
        "FROM torrent_contents WHERE file_id = $1",
        file_id,
    )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("tree"), str):
        d["tree"] = _json.loads(d["tree"])
    return d


async def save_torrent_tree(
    file_id: int,
    name: Optional[str],
    total_size: int,
    file_count: int,
    tree: list,
    error: Optional[str] = None,
) -> None:
    await _exec(
        """INSERT INTO torrent_contents
               (file_id, torrent_name, total_size, file_count, tree, parsed_at, error)
           VALUES ($1, $2, $3, $4, $5::jsonb, NOW(), $6)
           ON CONFLICT (file_id) DO UPDATE SET
               torrent_name = EXCLUDED.torrent_name,
               total_size   = EXCLUDED.total_size,
               file_count   = EXCLUDED.file_count,
               tree         = EXCLUDED.tree,
               parsed_at    = NOW(),
               error        = EXCLUDED.error""",
        file_id,
        name,
        total_size,
        file_count,
        _json.dumps(tree, ensure_ascii=False),
        error,
    )

    if tree and not error:
        # Rebuild normalized torrent_files entries for fast search.
        async with _pool.acquire() as conn:
            await conn.execute("DELETE FROM torrent_files WHERE torrent_id = $1", file_id)
            if tree:
                await conn.executemany(
                    "INSERT INTO torrent_files (torrent_id, path, size) VALUES ($1, $2, $3)",
                    [(file_id, f["path"], f.get("size", 0)) for f in tree],
                )
        # The torrent is just a pointer — expose the content size as the
        # file's canonical size throughout the entire application.
        if total_size > 0:
            await _exec(
                "UPDATE files SET file_size = $1 WHERE id = $2",
                total_size, file_id,
            )
            invalidate_files_caches()


async def get_unparsed_torrents(limit: int = 5000) -> List[Dict]:
    """Return files with ext='torrent' that have no entry in torrent_contents."""
    rows = await _q(
        """SELECT f.id, f.group_id, f.message_id, f.file_name,
                  f.discovered_by_account_id
           FROM files f
           LEFT JOIN torrent_contents tc ON tc.file_id = f.id
           WHERE LOWER(f.file_ext) = 'torrent'
             AND tc.file_id IS NULL
           ORDER BY f.date DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def count_torrents() -> Dict:
    total = (await _qval(
        "SELECT COUNT(*) FROM files WHERE LOWER(file_ext) = 'torrent'"
    )) or 0
    parsed = (await _qval(
        "SELECT COUNT(*) FROM torrent_contents WHERE error IS NULL"
    )) or 0
    errors = (await _qval(
        "SELECT COUNT(*) FROM torrent_contents WHERE error IS NOT NULL"
    )) or 0
    return {
        "total": total,
        "parsed": parsed,
        "errors": errors,
        "pending": max(0, total - parsed - errors),
    }


async def sync_torrent_files() -> int:
    """Populate torrent_files from existing torrent_contents and backfill
    files.file_size with content totals.  Safe to call on every startup —
    only touches rows not yet present in torrent_files."""
    import logging as _log
    log = _log.getLogger("database.torrent_sync")

    # Populate torrent_files for any torrent_contents entry not yet synced.
    await _exec("""
        INSERT INTO torrent_files (torrent_id, path, size)
        SELECT tc.file_id,
               e->>'path',
               COALESCE((e->>'size')::bigint, 0)
        FROM torrent_contents tc
        CROSS JOIN jsonb_array_elements(COALESCE(tc.tree, '[]'::jsonb)) AS e
        WHERE tc.error IS NULL
          AND jsonb_array_length(COALESCE(tc.tree, '[]'::jsonb)) > 0
          AND NOT EXISTS (
              SELECT 1 FROM torrent_files tf WHERE tf.torrent_id = tc.file_id
          )
    """)

    # Backfill files.file_size → torrent content total (skip if already set).
    updated = (await _qval("""
        WITH upd AS (
            UPDATE files f
            SET file_size = tc.total_size
            FROM torrent_contents tc
            WHERE tc.file_id = f.id
              AND tc.error IS NULL
              AND LOWER(f.file_ext) = 'torrent'
              AND tc.total_size > 0
              AND f.file_size <> tc.total_size
            RETURNING 1
        )
        SELECT COUNT(*) FROM upd
    """)) or 0

    if updated:
        invalidate_files_caches()
        log.info("torrent sync: updated file_size for %d torrent files", updated)

    tf_count = (await _qval("SELECT COUNT(*) FROM torrent_files")) or 0
    log.info("torrent sync: torrent_files table has %d entries", tf_count)
    return int(updated)


async def search_torrent_files(q: str, limit: int = 100) -> List[Dict]:
    """Full-text search inside torrent contents using the trigram-indexed
    torrent_files table.  Returns one row per matching .torrent file, with
    the matched paths aggregated."""
    rows = await _q(
        """SELECT
               f.id               AS torrent_file_id,
               f.file_name,
               f.file_size        AS content_size,
               f.date,
               COALESCE(g.display_name, g.name) AS group_name,
               g.username         AS group_username,
               tc.torrent_name,
               jsonb_agg(
                   jsonb_build_object('path', tf.path, 'size', tf.size)
                   ORDER BY tf.path
               )                  AS matched_paths
           FROM torrent_files tf
           JOIN files f  ON f.id  = tf.torrent_id
           JOIN groups g ON g.id  = f.group_id
           LEFT JOIN torrent_contents tc ON tc.file_id = f.id
           WHERE tf.path ILIKE $1
           GROUP BY f.id, f.file_name, f.file_size, f.date,
                    g.display_name, g.name, g.username, tc.torrent_name
           ORDER BY count(tf.id) DESC, f.date DESC
           LIMIT $2""",
        f"%{q}%",
        limit,
    )
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("matched_paths"), str):
            d["matched_paths"] = _json.loads(d["matched_paths"])
        result.append(d)
    return result


async def get_activity_heatmap(group_id: int = None) -> list:
    """Return file counts bucketed by day-of-week (0=Sun) and hour-of-day (UTC)."""
    if group_id is not None:
        rows = await _q(
            "SELECT EXTRACT(DOW FROM date AT TIME ZONE 'UTC')::int AS dow,"
            "       EXTRACT(HOUR FROM date AT TIME ZONE 'UTC')::int AS hour,"
            "       COUNT(*)::int AS cnt"
            "  FROM files WHERE group_id = $1"
            "  GROUP BY dow, hour",
            group_id,
        )
    else:
        rows = await _q(
            "SELECT EXTRACT(DOW FROM date AT TIME ZONE 'UTC')::int AS dow,"
            "       EXTRACT(HOUR FROM date AT TIME ZONE 'UTC')::int AS hour,"
            "       COUNT(*)::int AS cnt"
            "  FROM files GROUP BY dow, hour"
        )
    return [dict(r) for r in rows]


# ── Archive contents (link_archive_contents) ───────────────────────────────────

async def get_link_archive_contents(link_id: int) -> Dict[str, List]:
    """Return all inspected archive contents for a link as {path: [file_list]}."""
    rows = await _q(
        "SELECT archive_path, contents FROM link_archive_contents WHERE link_id = $1",
        link_id,
    )
    result: Dict[str, List] = {}
    for r in rows:
        contents = r["contents"]
        if isinstance(contents, str):
            import json as _j
            try:
                contents = _j.loads(contents)
            except Exception:
                contents = []
        result[r["archive_path"]] = contents or []
    return result


async def store_link_archive_contents(
    link_id: int,
    archive_path: str,
    contents: List[Dict],
) -> None:
    """Upsert the file listing for one archive inside a torrent link."""
    import json as _j
    await _exec(
        """INSERT INTO link_archive_contents (link_id, archive_path, contents, inspected_at)
           VALUES ($1, $2, $3::jsonb, NOW())
           ON CONFLICT (link_id, archive_path)
           DO UPDATE SET contents = EXCLUDED.contents, inspected_at = NOW()""",
        link_id,
        archive_path,
        _j.dumps(contents),
    )
