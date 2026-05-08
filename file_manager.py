"""Move video files between status folders and update DB rows.

Status flow (set_status):
    pending  ↔ approved
    pending  ↔ rejected
    approved ↔ rejected

The "deleted" state is reached via delete_video() (replaces video file with
a thumbnail JPEG) and exited via restore_video() (re-downloads from TikTok).
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

import config
import database

log = logging.getLogger(__name__)

# Statuses set_status() can move a video into.
VALID_STATUSES = ("pending", "approved", "rejected")

# All recognized statuses including the special "deleted" state.
ALL_STATUSES = ("pending", "approved", "rejected", "deleted")


def _root_for_status(status: str) -> Path:
    return {
        "pending":  config.DOWNLOADS_DIR,
        "approved": config.APPROVED_DIR,
        "rejected": config.REJECTED_DIR,
        "deleted":  config.DELETED_DIR,
    }[status]


def status_from_path(p: Path) -> str:
    """Infer status from which top-level folder a file lives under."""
    try:
        p = p.resolve()
    except OSError:
        return "pending"
    for status in ALL_STATUSES:
        try:
            p.relative_to(_root_for_status(status).resolve())
            return status
        except ValueError:
            continue
    return "pending"


# ---------------------------------------------------------------------------
# Thumbnails (used for the "deleted" state)
# ---------------------------------------------------------------------------
def generate_thumbnail(video_path: Path, jpg_path: Path) -> bool:
    """Pull a single frame near the start of ``video_path`` and write it as
    a downscaled JPEG to ``jpg_path``. Returns True on success."""
    jpg_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # -ss before -i is fast (seeks at container level); for very short
        # clips we fall back to no seek (first frame).
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", "0.5", "-i", str(video_path),
                "-frames:v", "1",
                "-vf", "scale=540:-2",
                "-q:v", "3",
                str(jpg_path),
            ],
            check=True, timeout=30,
        )
        if jpg_path.exists() and jpg_path.stat().st_size > 0:
            return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        log.warning("first-frame thumbnail failed for %s: %s", video_path.name, e)

    # Retry without the seek for ultra-short videos
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", "scale=540:-2",
                "-q:v", "3",
                str(jpg_path),
            ],
            check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        log.error("thumbnail generation failed for %s: %s", video_path.name, e)
        return False
    return jpg_path.exists() and jpg_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Deleted state
# ---------------------------------------------------------------------------
def delete_video(video_id: str) -> tuple[bool, str]:
    """Move a video to the deleted state: generate a thumbnail JPEG, remove
    the video file, update the DB row. The DB row itself is preserved so
    future scrapes still recognize this video_id and won't re-download it."""
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT video_id, file_path, source_user, status FROM videos WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            return False, f"video_id {video_id!r} not in DB"
        if row["status"] == "deleted":
            return True, "already deleted"

        old_path = Path(row["file_path"]) if row["file_path"] else None
        user_folder = (
            old_path.parent.name if old_path and old_path.parent != config.PROJECT_ROOT
            else (row["source_user"] or "_unknown").lstrip("@")
        )
        deleted_dir = config.DELETED_DIR / user_folder
        deleted_dir.mkdir(parents=True, exist_ok=True)

        if old_path and old_path.exists():
            thumb_path = deleted_dir / f"{old_path.stem}.jpg"
            if not generate_thumbnail(old_path, thumb_path):
                return False, "could not generate thumbnail; aborting delete"
            try:
                old_path.unlink()
            except OSError as e:
                # Roll back the thumbnail if we couldn't remove the original
                if thumb_path.exists():
                    try: thumb_path.unlink()
                    except OSError: pass
                return False, f"could not delete original file: {e}"
            new_file_path = str(thumb_path)
        else:
            # No video file to remove — just flip the status. file_path empty.
            new_file_path = ""

        conn.execute(
            "UPDATE videos SET status = 'deleted', file_path = ? WHERE video_id = ?",
            (new_file_path, video_id),
        )
        conn.commit()
        return True, "deleted"
    finally:
        conn.close()


def migrate_creator_files(handle: str, new_folder_name: str) -> dict:
    """Move every file belonging to ``handle`` (regardless of which
    historical folder it currently lives in) into a folder named
    ``new_folder_name`` under each status root. Updates DB file_paths to
    match. Cleans up any folders that end up empty afterwards.

    Returns counts: {moved, folders_cleaned}."""
    moved = 0
    folders_to_clean: set[Path] = set()

    conn = database.connect()
    try:
        rows = conn.execute(
            "SELECT video_id, file_path FROM videos WHERE source_user = ?",
            (handle,),
        ).fetchall()

        for r in rows:
            if not r["file_path"]:
                continue
            current = Path(r["file_path"])
            if not current.exists():
                continue
            # status root is the grandparent (e.g. downloads/<old>/<file> -> downloads/)
            status_root = current.parent.parent
            new_dir = status_root / new_folder_name
            target = new_dir / current.name

            # Already in the right place — skip silently
            try:
                if current.resolve() == target.resolve():
                    continue
            except OSError:
                pass

            new_dir.mkdir(parents=True, exist_ok=True)

            if target.exists():
                log.warning("rename target exists, skipping: %s", target)
                continue

            try:
                current.rename(target)
            except OSError as e:
                log.warning("could not move %s -> %s: %s", current, target, e)
                continue

            folders_to_clean.add(current.parent)
            conn.execute(
                "UPDATE videos SET file_path = ? WHERE video_id = ?",
                (str(target), r["video_id"]),
            )
            moved += 1

        conn.commit()
    finally:
        conn.close()

    folders_cleaned = 0
    for f in folders_to_clean:
        try:
            if f.exists() and not any(f.iterdir()):
                f.rmdir()
                folders_cleaned += 1
        except OSError as e:
            log.warning("could not remove empty folder %s: %s", f, e)

    log.info("migrated %s -> %r: %d files, %d folders cleaned",
             handle, new_folder_name, moved, folders_cleaned)
    return {"moved": moved, "folders_cleaned": folders_cleaned}


def purge_creator_videos(handle: str, *, keep_approved: bool) -> dict:
    """Remove videos for ``handle`` from disk and DB. When ``keep_approved``
    is True, approved videos stay; everything else (pending, rejected,
    deleted-thumbnails) is wiped. Empty user folders are cleaned up too.

    Returns counts: {files_deleted, rows_deleted, folders_removed}."""
    statuses_to_purge = ["pending", "rejected", "deleted"]
    if not keep_approved:
        statuses_to_purge.append("approved")

    files_deleted = 0
    rows_deleted = 0
    folders_to_check: set[Path] = set()

    conn = database.connect()
    try:
        rows = conn.execute(
            "SELECT video_id, file_path, status FROM videos WHERE source_user = ?",
            (handle,),
        ).fetchall()

        for r in rows:
            if r["status"] not in statuses_to_purge:
                continue
            if r["file_path"]:
                p = Path(r["file_path"])
                folders_to_check.add(p.parent)
                if p.exists():
                    try:
                        p.unlink()
                        files_deleted += 1
                    except OSError as e:
                        log.warning("could not delete %s: %s", p, e)
            conn.execute("DELETE FROM videos WHERE video_id = ?", (r["video_id"],))
            rows_deleted += 1

        conn.commit()
    finally:
        conn.close()

    # Sweep any now-empty user folders
    folders_removed = 0
    for f in folders_to_check:
        try:
            if f.exists() and not any(f.iterdir()):
                f.rmdir()
                folders_removed += 1
        except OSError as e:
            log.warning("could not remove empty folder %s: %s", f, e)

    log.info("purge %s (keep_approved=%s): %d files, %d rows, %d folders",
             handle, keep_approved, files_deleted, rows_deleted, folders_removed)
    return {
        "files_deleted": files_deleted,
        "rows_deleted": rows_deleted,
        "folders_removed": folders_removed,
    }


def restore_video(video_id: str) -> tuple[bool, str]:
    """Re-download a deleted video. Sets status back to 'pending' and removes
    the thumbnail JPEG. Imports inside the function to avoid a circular
    import with downloader."""
    from downloader import download_video  # local to avoid circular import

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT video_id, file_path, source_user, status FROM videos WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            return False, "video not in DB"
        if row["status"] != "deleted":
            return False, f"video is {row['status']!r}, not deleted"

        source_user = row["source_user"] or ""
        if not source_user:
            return False, "missing source_user — cannot rebuild URL"
        handle = source_user.lstrip("@")
        url = f"https://www.tiktok.com/@{handle}/video/{video_id}"

        thumb_path = Path(row["file_path"]) if row["file_path"] else None
        # Folder name to download into: preserve whichever folder was used
        # before deletion.
        folder_label = thumb_path.parent.name if thumb_path else handle

    finally:
        conn.close()

    try:
        new_path, info = download_video(url, folder_label, video_id=video_id, quiet=True)
    except Exception as e:
        return False, f"download failed: {e}"
    if not new_path:
        return False, "download returned no file (may have been removed from TikTok)"

    # Clean up the thumbnail (best-effort)
    if thumb_path and thumb_path.exists() and thumb_path.suffix.lower() == ".jpg":
        try: thumb_path.unlink()
        except OSError as e:
            log.warning("could not delete thumbnail %s: %s", thumb_path, e)

    conn = database.connect()
    try:
        new_dur = info.get("duration") if isinstance(info, dict) else None
        if new_dur:
            conn.execute(
                "UPDATE videos SET status = 'pending', file_path = ?, "
                "duration_seconds = ? WHERE video_id = ?",
                (str(new_path), int(new_dur), video_id),
            )
        else:
            conn.execute(
                "UPDATE videos SET status = 'pending', file_path = ? WHERE video_id = ?",
                (str(new_path), video_id),
            )
        conn.commit()
    finally:
        conn.close()
    return True, "restored"


def set_status(video_id: str, new_status: str) -> tuple[bool, str]:
    """Move the file for ``video_id`` into the folder matching ``new_status``
    and update its DB row. Returns (success, message)."""
    if new_status not in VALID_STATUSES:
        return False, f"invalid status: {new_status!r}"

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT video_id, file_path, source_user, status FROM videos WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            return False, f"video_id {video_id!r} not in DB"

        current_path = Path(row["file_path"]) if row["file_path"] else None
        target_root = _root_for_status(new_status)

        # Preserve the user subfolder. If the existing path doesn't have one
        # (legacy), fall back to source_user with the @ stripped.
        if current_path and current_path.parent != config.PROJECT_ROOT:
            user_folder_name = current_path.parent.name
        else:
            user_folder_name = (row["source_user"] or "_unknown").lstrip("@")

        target_dir = target_root / user_folder_name
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path: Path
        if current_path and current_path.exists():
            target_path = target_dir / current_path.name
            if current_path.resolve() != target_path.resolve():
                # Windows can briefly hold a lock on the file even after the
                # browser closes its HTTP connection (Flask's send_file +
                # OS file cache). Retry with backoff before giving up.
                last_err: Optional[Exception] = None
                delay = 0.05
                for attempt in range(6):
                    try:
                        current_path.rename(target_path)
                        log.info("moved %s -> %s", current_path, target_path)
                        last_err = None
                        break
                    except OSError as e:
                        last_err = e
                        time.sleep(delay)
                        delay = min(delay * 2, 0.5)
                if last_err is not None:
                    return False, f"could not move file: {last_err}"
            else:
                target_path = current_path  # already there
        else:
            # File missing — update DB anyway, leave file_path pointing at
            # where it would have been.
            target_path = target_dir / (current_path.name if current_path else f"{video_id}.mp4")
            log.warning("file not found for %s; updating DB only", video_id)

        conn.execute(
            "UPDATE videos SET status = ?, file_path = ? WHERE video_id = ?",
            (new_status, str(target_path), video_id),
        )
        conn.commit()
        return True, f"set {video_id} -> {new_status}"
    finally:
        conn.close()
