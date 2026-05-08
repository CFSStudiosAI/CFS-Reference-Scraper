"""SQLite layer for the Dance Scraper.

One table:
    videos — one row per downloaded clip (PK = TikTok video_id)

Public surface:
    connect()                            -> sqlite3.Connection
    init_db(conn=None)                   -> creates schema + runs migrations
    has_video(conn, video_id)            -> bool
    insert_video(conn, video, ...)       -> inserts a video row
    list_videos(conn, source_user=...)   -> for browsing
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger(__name__)


_TABLE = """
CREATE TABLE IF NOT EXISTS videos (
    video_id          TEXT    PRIMARY KEY,
    title             TEXT    NOT NULL,
    channel           TEXT,
    source_user       TEXT    DEFAULT '',
    duration_seconds  INTEGER,
    view_count        INTEGER,
    upload_date       TEXT,
    download_date     TEXT,
    file_path         TEXT,
    status            TEXT    DEFAULT 'pending'
);
"""

# Built after migrations so indices on newly-added columns succeed.
_INDICES = """
CREATE INDEX IF NOT EXISTS idx_videos_source_user ON videos(source_user);
CREATE INDEX IF NOT EXISTS idx_videos_status      ON videos(status);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and lazily create) the SQLite database."""
    path = Path(db_path) if db_path else config.DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema migrations. SQLite 3.35+ supports DROP COLUMN."""
    cur = conn.execute("PRAGMA table_info(videos)")
    cols = {row[1] for row in cur.fetchall()}

    # Drop unused columns
    for col in ("tags", "notes"):
        if col in cols:
            try:
                conn.execute(f"ALTER TABLE videos DROP COLUMN {col}")
                log.info("migrated: dropped videos.%s column", col)
            except sqlite3.OperationalError as e:
                log.warning("could not drop column %s: %s", col, e)

    # Add status column for approve/reject workflow
    if "status" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN status TEXT DEFAULT 'pending'")
        log.info("migrated: added videos.status column")

    conn.commit()


def init_db(conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    if conn is None:
        conn = connect()
    conn.executescript(_TABLE)
    conn.commit()
    _migrate(conn)
    conn.executescript(_INDICES)
    conn.commit()
    log.info("database ready: %s", config.DATABASE_PATH)
    return conn


def _pluck(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def has_video(conn: sqlite3.Connection, video_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM videos WHERE video_id = ? LIMIT 1", (video_id,))
    return cur.fetchone() is not None


def insert_video(
    conn: sqlite3.Connection,
    video: Any,
    *,
    source_user: str = "",
    file_path: str = "",
    status: str = "pending",
    download_date: Optional[str] = None,
) -> bool:
    """Insert a video row. Returns True on insert, False if the video_id was
    already present."""
    video_id = _pluck(video, "video_id")
    if not video_id:
        raise ValueError("video has no video_id")

    if download_date is None:
        download_date = date.today().isoformat()

    row = {
        "video_id":         video_id,
        "title":            _pluck(video, "title", ""),
        "channel":          _pluck(video, "channel", ""),
        "source_user":      source_user or _pluck(video, "source_user", ""),
        "duration_seconds": _pluck(video, "duration_seconds"),
        "view_count":       _pluck(video, "view_count"),
        "upload_date":      _pluck(video, "upload_date", ""),
        "download_date":    download_date,
        "file_path":        file_path,
        "status":           status,
    }

    try:
        conn.execute(
            """
            INSERT INTO videos (
                video_id, title, channel, source_user,
                duration_seconds, view_count, upload_date, download_date,
                file_path, status
            ) VALUES (
                :video_id, :title, :channel, :source_user,
                :duration_seconds, :view_count, :upload_date, :download_date,
                :file_path, :status
            )
            """,
            row,
        )
    except sqlite3.IntegrityError:
        log.debug("video %s already exists, skipping insert", video_id)
        return False
    conn.commit()
    return True


def get_video(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    return cur.fetchone()


def list_videos(
    conn: sqlite3.Connection,
    *,
    source_user: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM videos"
    params: list[Any] = []
    if source_user is not None:
        sql += " WHERE source_user = ?"
        params.append(source_user)
    sql += " ORDER BY upload_date DESC, download_date DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print(f"DB path: {config.DATABASE_PATH}")
    conn = init_db()

    fake_video_id = "__TEST_FAKE_ID__"
    conn.execute("DELETE FROM videos WHERE video_id = ?", (fake_video_id,))
    conn.commit()

    inserted = insert_video(
        conn,
        {
            "video_id": fake_video_id,
            "title": "Fake Test Video",
            "channel": "Test",
            "duration_seconds": 12,
            "view_count": 1234,
            "upload_date": "2026-01-01",
        },
        source_user="@test_handle",
        file_path="downloads/Test/__TEST_FAKE_ID__.mp4",
    )
    print(f"insert: {inserted}, has_video: {has_video(conn, fake_video_id)}")

    row = get_video(conn, fake_video_id)
    print(f"row keys: {list(row.keys())}")

    conn.execute("DELETE FROM videos WHERE video_id = ?", (fake_video_id,))
    conn.commit()
    conn.close()
    print("OK")
