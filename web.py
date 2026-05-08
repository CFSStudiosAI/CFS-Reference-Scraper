"""Local browse UI for the dance scraper library.

Run with `browse.bat` or:
    python web.py

Browser opens automatically at http://127.0.0.1:5000/.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

import config
import database
import downloader
import file_manager
from downloader import sanitize_filename


app = Flask(__name__)
app.secret_key = "dance-scraper-local-only"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _format_bytes(b: int) -> str:
    f = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


app.jinja_env.filters["filesize"] = _format_bytes


def _format_days_ago(days) -> str:
    if days is None:
        return "—"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


app.jinja_env.filters["days_ago"] = _format_days_ago


def _creator_stats() -> dict[str, dict]:
    """Per-creator counts and last-upload age, keyed by source_user handle."""
    today = date.today()
    conn = database.connect()
    try:
        rows = conn.execute(
            """
            SELECT source_user,
                   COUNT(*)                                                   AS total,
                   SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END)       AS approved,
                   SUM(CASE WHEN status IN ('rejected','deleted') THEN 1 ELSE 0 END) AS rejected,
                   MAX(upload_date)                                           AS last_upload
            FROM videos
            WHERE source_user != ''
            GROUP BY source_user
            """
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for r in rows:
        total = r["total"] or 0
        approved = r["approved"] or 0
        rejected = r["rejected"] or 0
        days_ago = None
        last_upload = r["last_upload"]
        if last_upload:
            try:
                days_ago = max(0, (today - date.fromisoformat(last_upload)).days)
            except ValueError:
                pass
        # Approval % = approved out of *graded* (approved + rejected/deleted).
        # Pending videos don't count yet — they haven't been judged.
        graded = approved + rejected
        out[r["source_user"]] = {
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "graded": graded,
            "approval_pct": round(100 * approved / graded) if graded else 0,
            "days_ago": days_ago,
        }
    return out


def _read_users_csv() -> list[dict]:
    """Return the current rows from tiktok_users.csv minus placeholders."""
    out: list[dict] = []
    if not config.USERS_CSV.exists():
        return out
    with config.USERS_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            h = (r.get("handle") or "").strip()
            if not h:
                continue
            if h.startswith("@example_") or "replace_me" in h.lower():
                continue
            out.append({
                "handle": h,
                "name": (r.get("name") or "").strip(),
            })
    return out


def _write_users_csv(rows: list[dict]) -> None:
    with config.USERS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["handle", "name"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "handle": r.get("handle", ""),
                "name": r.get("name", ""),
            })


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    user_filter = (request.args.get("user") or "").strip() or None
    status_filter = (request.args.get("status") or "pending").strip().lower()
    if status_filter not in ("pending", "approved", "rejected", "deleted"):
        status_filter = "pending"
    query = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    page_size = config.PAGE_SIZE

    where: list[str] = ["status = ?"]
    params: list = [status_filter]
    if user_filter:
        where.append("source_user = ?")
        params.append(user_filter)
    if query:
        where.append("title LIKE ?")
        params.append(f"%{query}%")
    where_sql = " WHERE " + " AND ".join(where)

    conn = database.connect()
    try:
        # Count of all rows matching the current filter (drives pagination)
        total_filtered = conn.execute(
            f"SELECT COUNT(*) FROM videos{where_sql}", params
        ).fetchone()[0]

        total_pages = max(1, (total_filtered + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        rows = conn.execute(
            f"SELECT * FROM videos{where_sql} "
            f"ORDER BY upload_date DESC, download_date DESC "
            f"LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

        users_in_db = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_user FROM videos "
                "WHERE source_user != '' ORDER BY source_user"
            ).fetchall()
        ]
        total_count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

        # Per-status counts for the tab badges (respect user filter only)
        status_counts: dict[str, int] = {"pending": 0, "approved": 0,
                                         "rejected": 0, "deleted": 0}
        if user_filter:
            sc_rows = conn.execute(
                "SELECT status, COUNT(*) FROM videos WHERE source_user = ? GROUP BY status",
                (user_filter,),
            ).fetchall()
        else:
            sc_rows = conn.execute(
                "SELECT status, COUNT(*) FROM videos GROUP BY status"
            ).fetchall()
        for s, c in sc_rows:
            status_counts[(s or "pending")] = c

        # Library-wide disk usage (independent of filter — gives a stable
        # "how big is everything" stat in the header)
        all_paths = conn.execute("SELECT file_path FROM videos").fetchall()
    finally:
        conn.close()

    creators = _read_users_csv()
    csv_users = [u["handle"] for u in creators]
    all_users = sorted(set(users_in_db) | set(csv_users))

    # handle -> friendly name override (for display only — filter values
    # and DB keys still use the raw @handle)
    display_names = {c["handle"]: c["name"] for c in creators if c.get("name")}

    total_bytes = 0
    for r in all_paths:
        p = Path(r["file_path"]) if r["file_path"] else None
        if p and p.exists():
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass

    return render_template(
        "index.html",
        videos=[dict(r) for r in rows],
        users=all_users,
        total_count=total_count,
        total_filtered=total_filtered,
        total_bytes=total_bytes,
        current_user=user_filter,
        current_status=status_filter,
        current_query=query,
        status_counts=status_counts,
        page=page,
        total_pages=total_pages,
        page_size=page_size,
        creators=creators,
        display_names=display_names,
        creator_stats=_creator_stats(),
    )


@app.route("/video/<video_id>")
def serve_video(video_id: str):
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT file_path FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["file_path"]:
        abort(404)
    p = Path(row["file_path"])
    if not p.exists():
        abort(404)
    return send_file(p, conditional=True)  # supports range requests for seeking


@app.route("/trim/<video_id>", methods=["POST"])
def trim_video(video_id: str):
    """Trim a video to [start, end] seconds. Re-encodes in place; updates
    duration in the DB. Body: JSON {"start": float, "end": float}."""
    data = request.get_json(silent=True) or {}
    try:
        start = float(data.get("start", 0))
        end = float(data.get("end", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid start/end"}), 400

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT file_path FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        if not row or not row["file_path"]:
            return jsonify({"ok": False, "error": "video not in DB"}), 404
        path = Path(row["file_path"])
        if not path.exists():
            return jsonify({"ok": False, "error": "file missing on disk"}), 404

        ok, msg = downloader.trim_to(path, start, end)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 500

        new_duration = max(1, int(round(end - start)))
        conn.execute(
            "UPDATE videos SET duration_seconds = ? WHERE video_id = ?",
            (new_duration, video_id),
        )
        conn.commit()
        return jsonify({"ok": True, "duration": new_duration})
    finally:
        conn.close()


@app.route("/rate/<video_id>", methods=["POST"])
def rate_video(video_id: str):
    """Set a video's status. Body: JSON {"status": "approved"|"rejected"|"pending"}.
    'deleted' is intentionally not accepted here — use /delete_rejected."""
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip().lower()
    if new_status == "deleted":
        return jsonify({"ok": False, "error": "use /delete_rejected for deletion"}), 400
    ok, msg = file_manager.set_status(video_id, new_status)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "status": new_status})


@app.route("/delete_rejected", methods=["POST"])
def delete_rejected():
    """Bulk-delete every rejected video, scoped to ``user`` if provided.
    Each deletion replaces the video file with a thumbnail JPEG and flips
    its DB status to 'deleted'. The DB row stays so the scraper still
    skips it on subsequent runs."""
    user_filter = (request.form.get("user") or "").strip() or None

    conn = database.connect()
    try:
        if user_filter:
            ids = [
                r[0] for r in conn.execute(
                    "SELECT video_id FROM videos WHERE status = 'rejected' AND source_user = ?",
                    (user_filter,),
                ).fetchall()
            ]
        else:
            ids = [
                r[0] for r in conn.execute(
                    "SELECT video_id FROM videos WHERE status = 'rejected'"
                ).fetchall()
            ]
    finally:
        conn.close()

    deleted_n = 0
    failed_n = 0
    for vid in ids:
        ok, _ = file_manager.delete_video(vid)
        if ok: deleted_n += 1
        else: failed_n += 1

    if deleted_n:
        flash(f"Deleted {deleted_n} rejected video(s).", "success")
    if failed_n:
        flash(f"{failed_n} could not be deleted (see logs).", "error")
    if not ids:
        flash("No rejected videos to delete.", "error")

    # Send the user to the deleted view so they see what just moved
    return redirect(url_for("index", user=user_filter, status="deleted"))


@app.route("/restore/<video_id>", methods=["POST"])
def restore_video_route(video_id: str):
    """Re-download a deleted video. Returns JSON; the UI can update in place."""
    ok, msg = file_manager.restore_video(video_id)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True})


@app.route("/add_user", methods=["POST"])
def add_user():
    handle = (request.form.get("handle") or "").strip()
    name = (request.form.get("name") or "").strip()

    if not handle:
        flash("Handle is required.", "error")
        return redirect(url_for("index"))

    if not handle.startswith("@"):
        handle = "@" + handle.lstrip("@")

    rows = _read_users_csv()
    if any(r["handle"].lower() == handle.lower() for r in rows):
        flash(f"{handle} is already tracked.", "error")
        return redirect(url_for("index"))

    rows.append({"handle": handle, "name": name})
    _write_users_csv(rows)
    flash(f"Added {handle}. Run start.bat to download their videos.", "success")
    return redirect(url_for("index"))


@app.route("/update_user", methods=["POST"])
def update_user():
    """Change the friendly `name` of an existing creator. Migrates all
    existing files for this handle into the new folder name, across every
    status root, and removes any now-empty old folders."""
    handle = (request.form.get("handle") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not handle:
        flash("Missing handle.", "error")
        return redirect(url_for("index"))

    rows = _read_users_csv()
    target_row = next(
        (r for r in rows if r["handle"].lower() == handle.lower()), None
    )
    if target_row is None:
        flash(f"{handle} not in list.", "error")
        return redirect(url_for("index"))

    target_row["name"] = name
    _write_users_csv(rows)

    # Migrate any existing files for this handle into the new folder name.
    new_folder = sanitize_filename(name or handle)
    result = file_manager.migrate_creator_files(handle, new_folder)

    bits = [f"Updated {handle}."]
    if result["moved"]:
        bits.append(f"Moved {result['moved']} file(s) to '{new_folder}'.")
    if result["folders_cleaned"]:
        bits.append(f"Cleaned {result['folders_cleaned']} empty folder(s).")
    flash(" ".join(bits), "success")
    return redirect(url_for("index"))


@app.route("/delete_user", methods=["POST"])
def delete_user():
    """Remove a creator from tiktok_users.csv AND purge their videos.

    Modes:
      keep_approved — wipe pending/rejected/deleted; keep approved
      purge_all     — wipe everything for this creator
    """
    handle = (request.form.get("handle") or "").strip()
    mode = (request.form.get("mode") or "").strip()
    if not handle:
        flash("Missing handle.", "error")
        return redirect(url_for("index"))
    if mode not in ("keep_approved", "purge_all"):
        flash("Missing or invalid delete mode.", "error")
        return redirect(url_for("index"))

    # Remove from CSV (no error if not present — they may have already been
    # removed once and the user is just cleaning up videos)
    rows = _read_users_csv()
    new_rows = [r for r in rows if r["handle"].lower() != handle.lower()]
    _write_users_csv(new_rows)

    keep_approved = mode == "keep_approved"
    result = file_manager.purge_creator_videos(handle, keep_approved=keep_approved)

    bits = [f"Removed {handle}."]
    if result["files_deleted"]:
        bits.append(
            f"Deleted {result['files_deleted']} video file(s)"
            f" ({'kept approved' if keep_approved else 'including approved'})."
        )
    if result["folders_removed"]:
        bits.append(f"Removed {result['folders_removed']} empty folder(s).")
    flash(" ".join(bits), "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _open_browser_when_ready():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000/")


def _kick_off_scraper() -> None:
    """Launch scraper.py as a detached background process. Console output
    is discarded; the scraper's own file handler writes to logs/scraper_<date>.log
    so progress is observable there. Refreshing the browser shows new
    videos as they finish downloading."""
    log_path = config.LOGS_DIR / f"scraper_{date.today().isoformat()}.log"
    try:
        subprocess.Popen(
            [sys.executable, "scraper.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(config.PROJECT_ROOT),
        )
        print(f"[startup] background scrape running, log: {log_path}")
    except Exception as e:
        print(f"[startup] could not launch scraper: {e}")


if __name__ == "__main__":
    print()
    print("=" * 60)
    print("  CFSStudios.AI TT Scraper")
    print("  http://127.0.0.1:5000/")
    print("  Press Ctrl+C to stop the server")
    print("=" * 60)
    print()
    # Ensure the SQLite schema exists BEFORE Flask starts serving requests.
    # On a fresh clone, the background scraper might not run init_db() in
    # time (especially when tiktok_users.csv has only the placeholder row,
    # which makes the scraper exit early before touching the DB).
    database.init_db()
    _kick_off_scraper()
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
