"""
One-time migration: SQLite → PostgreSQL.
Run inside the container:
  docker exec telfiles-app python migrate.py
Groups and files are preserved. Links are reset (will be re-indexed from scratch).
"""
import asyncio
import sqlite3
import os
import asyncpg
from datetime import datetime, timezone

SQLITE_PATH = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "telfiles.db")
PG_URL = os.environ.get("DATABASE_URL", "postgresql://telfiles:telfiles@postgres/telfiles")


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def migrate():
    print(f"Connecting to SQLite: {SQLITE_PATH}")
    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = sqlite3.Row

    print(f"Connecting to PostgreSQL: {PG_URL}")
    pg = await asyncpg.connect(PG_URL)

    # ── Groups ────────────────────────────────────────────────────────────────
    groups = sq.execute("SELECT * FROM groups").fetchall()
    print(f"Migrating {len(groups)} groups…")
    for g in groups:
        await pg.execute(
            """INSERT INTO groups
               (id, name, username, is_channel, last_synced_message_id, last_synced_at,
                display_name, excluded, hidden, last_link_message_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (id) DO UPDATE SET
                   name = EXCLUDED.name,
                   username = EXCLUDED.username,
                   last_synced_message_id = EXCLUDED.last_synced_message_id,
                   last_synced_at = EXCLUDED.last_synced_at,
                   display_name = EXCLUDED.display_name,
                   excluded = EXCLUDED.excluded,
                   last_link_message_id = 0""",
            g["id"],
            g["name"],
            g["username"],
            bool(g["is_channel"]),
            g["last_synced_message_id"] or 0,
            parse_ts(g["last_synced_at"]),
            g["display_name"],
            bool(g["excluded"]) if g["excluded"] is not None else False,
            bool(g["hidden"]) if "hidden" in g.keys() and g["hidden"] is not None else False,
            0,  # reset last_link_message_id so links get re-indexed
        )
    print(f"  ✓ {len(groups)} groups migrated")

    # ── Files ─────────────────────────────────────────────────────────────────
    total_files = sq.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"Migrating {total_files} files (batch 10 000)…")

    batch_size = 10_000
    offset = 0
    migrated = 0
    while True:
        rows = sq.execute(
            "SELECT * FROM files ORDER BY id LIMIT ? OFFSET ?", (batch_size, offset)
        ).fetchall()
        if not rows:
            break
        async with pg.transaction():
            for r in rows:
                date_ts = parse_ts(r["date"]) or datetime.utcnow().replace(tzinfo=timezone.utc)
                context = r["context"] if "context" in r.keys() else None
                try:
                    await pg.execute(
                        """INSERT INTO files
                           (group_id, message_id, file_name, file_ext, mime_type,
                            file_size, date, local_path, downloaded_at,
                            downloading, download_progress, context)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                           ON CONFLICT (group_id, message_id) DO NOTHING""",
                        r["group_id"], r["message_id"], r["file_name"], r["file_ext"],
                        r["mime_type"], r["file_size"] or 0, date_ts,
                        r["local_path"], parse_ts(r["downloaded_at"]),
                        bool(r["downloading"]),
                        float(r["download_progress"] or 0),
                        context,
                    )
                    migrated += 1
                except Exception as e:
                    print(f"  Skip file {r['id']}: {e}")
        offset += batch_size
        print(f"  {migrated}/{total_files}…")

    print(f"  ✓ {migrated} files migrated")
    print("Links will be re-indexed on next sync (last_link_message_id reset to 0).")

    sq.close()
    await pg.close()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
