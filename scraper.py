"""Main orchestrator (TikTok mode).

Reads input/tiktok_users.csv, lists each user's videos via yt-dlp's
flat-playlist mode, downloads new ones (DB dedup), records metadata in
library.db.

Run with `start.bat` or:
    python scraper.py
"""

from __future__ import annotations

import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import config
import database
import file_manager
from downloader import download_video, existing_file_for, sanitize_filename
from tiktok_extractor import list_user_videos, TikTokVideo, UserPull

log = logging.getLogger("scraper")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> Path:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOGS_DIR / f"scraper_{date.today().isoformat()}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    con_h = logging.StreamHandler(sys.stdout)
    con_h.setFormatter(fmt)
    root.addHandler(con_h)

    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    return log_path


# ---------------------------------------------------------------------------
# CSV input
# ---------------------------------------------------------------------------
@dataclass
class UserRow:
    handle: str         # @handle from CSV
    name: str = ""      # optional friendly override; blank = use handle


def read_users_csv(path: Path = config.USERS_CSV) -> list[UserRow]:
    if not path.exists():
        raise FileNotFoundError(
            f"users CSV not found at {path}. Add @handles "
            f"(format: handle,name — one per row)."
        )
    out: list[UserRow] = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            handle = (row.get("handle") or "").strip()
            if not handle:
                log.warning("CSV row %d has no handle — skipping", i)
                continue
            if handle.startswith("@example_") or "replace_me" in handle.lower():
                log.warning("CSV row %d is the placeholder (%r) — skipping",
                            i, handle)
                continue
            out.append(UserRow(
                handle=handle,
                name=(row.get("name") or "").strip(),
            ))
    return out


# ---------------------------------------------------------------------------
# Run stats
# ---------------------------------------------------------------------------
@dataclass
class RunStats:
    users_processed:    int = 0
    users_unresolved:   int = 0
    listed_videos:      int = 0
    downloaded:         int = 0
    skipped_in_db:      int = 0
    skipped_on_disk:    int = 0
    download_failures:  int = 0
    per_user:           dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _format_upload_date(info: dict) -> str:
    """yt-dlp gives upload_date as YYYYMMDD or sometimes a unix timestamp."""
    raw = info.get("upload_date")
    if raw and isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    ts = info.get("timestamp")
    if ts:
        try:
            return datetime.utcfromtimestamp(int(ts)).date().isoformat()
        except (TypeError, ValueError):
            pass
    return ""


def _build_video_row(v: TikTokVideo, info: dict) -> dict:
    """Merge what we knew from the listing with what yt-dlp returned after
    download. Used as input to database.insert_video()."""
    title = v.title or info.get("title", "") or ""
    duration = info.get("duration") or v.duration_seconds or 0
    views = info.get("view_count") or v.view_count or 0
    upload_date = _format_upload_date(info) or v.upload_date or ""
    return {
        "video_id":         v.video_id,
        "title":            title[:500],
        "channel":          info.get("uploader") or v.source_user,
        "duration_seconds": int(duration) if duration else 0,
        "view_count":       int(views) if views else 0,
        "upload_date":      upload_date,
        "source_user":      v.source_user,
    }


# ---------------------------------------------------------------------------
# Per-user processing
# ---------------------------------------------------------------------------
def process_user(conn, user: UserRow, stats: RunStats) -> None:
    log.info("─" * 60)
    label_log = user.handle + (f"  ({user.name})" if user.name else "")
    log.info("USER: %s", label_log)

    pull: UserPull | None = list_user_videos(user.handle, max_count=config.VIDEOS_PER_USER)
    if pull is None:
        log.error("could not list videos for %r — skipping", user.handle)
        stats.users_unresolved += 1
        return

    stats.listed_videos += len(pull.videos)
    folder_label = sanitize_filename(user.name or pull.handle)

    if not pull.videos:
        log.warning("no videos found for %s", pull.handle)
        stats.users_processed += 1
        stats.per_user[folder_label] = 0
        return

    downloaded_here = 0
    for v in pull.videos:
        # DB dedup
        if database.has_video(conn, v.video_id):
            log.info("skip [%s] — already in DB", v.video_id)
            stats.skipped_in_db += 1
            continue

        # Disk dedup heal — also recovers approved/rejected files into the DB
        # with their correct status, inferred from which folder holds the file.
        on_disk = existing_file_for(folder_label, v.video_id)
        if on_disk:
            inferred = file_manager.status_from_path(on_disk)
            log.info("skip download [%s] — file exists in %s/, healing DB row",
                     v.video_id, inferred)
            row = _build_video_row(v, {})
            database.insert_video(
                conn, row,
                file_path=str(on_disk),
                source_user=v.source_user,
                status=inferred,
            )
            stats.skipped_on_disk += 1
            continue

        # Download
        try:
            path, info = download_video(
                v.url, folder_label,
                video_id=v.video_id,
                quiet=True,
            )
        except Exception as e:
            log.error("download failed [%s]: %s", v.video_id, e)
            stats.download_failures += 1
            continue

        if path is None:
            log.error("download returned no path for [%s]", v.video_id)
            stats.download_failures += 1
            continue

        row = _build_video_row(v, info or {})
        database.insert_video(
            conn, row,
            file_path=str(path),
            source_user=v.source_user,
        )
        downloaded_here += 1
        stats.downloaded += 1
        log.info("downloaded [%s] %ds  %s",
                 v.video_id, row["duration_seconds"], row["title"][:70])

    stats.per_user[folder_label] = downloaded_here
    stats.users_processed += 1


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _format_bytes(b: int) -> str:
    f = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def _disk_usage(root: Path) -> tuple[int, int]:
    """(total_bytes, file_count) of all non-partial files under ``root``."""
    total = 0
    count = 0
    if not root.exists():
        return 0, 0
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith(".part"):
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def print_summary(stats: RunStats, log_path: Path) -> None:
    print()
    print("=" * 60)
    print("  RUN SUMMARY")
    print("=" * 60)
    print(f"  Users processed       : {stats.users_processed}")
    if stats.users_unresolved:
        print(f"  Users unresolved      : {stats.users_unresolved}")
    print(f"  Videos listed         : {stats.listed_videos}")
    print(f"  Downloaded            : {stats.downloaded}")
    print(f"  Already in DB         : {stats.skipped_in_db}")
    if stats.skipped_on_disk:
        print(f"  Healed from disk      : {stats.skipped_on_disk}")
    if stats.download_failures:
        print(f"  Download failures     : {stats.download_failures}")
    print()
    if stats.per_user:
        print("  Per-user new downloads:")
        for label, n in stats.per_user.items():
            marker = "✓" if n > 0 else "·"
            print(f"    {marker} {n}  {label}")
    print()
    total_bytes, total_files = _disk_usage(config.DOWNLOADS_DIR)
    print(f"  Library total: {total_files} files, {_format_bytes(total_bytes)}")
    print(f"  Log file:      {log_path}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    log_path = setup_logging()
    log.info("scraper started, logging to %s", log_path)

    try:
        users = read_users_csv()
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2

    if not users:
        log.error(
            "No users in %s. Add at least one row with a real @handle "
            "and try again.",
            config.USERS_CSV,
        )
        return 2

    log.info("loaded %d user(s) from %s", len(users), config.USERS_CSV)
    conn = database.init_db()
    stats = RunStats()

    try:
        for u in users:
            process_user(conn, u, stats)
    except KeyboardInterrupt:
        log.warning("interrupted by user")
    finally:
        conn.close()

    print_summary(stats, log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
