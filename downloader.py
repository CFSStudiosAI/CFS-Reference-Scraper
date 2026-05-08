"""yt-dlp wrapper.

Public surface:
    download_video(url, folder_label, *, video_id=None) -> tuple[Path, dict]
    sanitize_filename(s)                                -> str
    existing_file_for(folder_label, video_id)           -> Path | None

Files land at:
    config.DOWNLOADS_DIR / <sanitized folder_label> / <upload_date>_<video_id>.<ext>

Resolution is capped via ``config.MAX_RESOLUTION``. Returns the yt-dlp
info_dict so the caller can persist metadata without a second extraction.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import config

log = logging.getLogger(__name__)

# Windows-reserved characters + control chars.
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(s: str, max_len: int = 80) -> str:
    """Make a string safe for use as a Windows folder or file name."""
    s = (s or "").strip()
    s = _INVALID_FS_CHARS.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "untitled"
    return s[:max_len].rstrip(" .") or "untitled"


def _folder_for(folder_label: str) -> Path:
    folder = config.DOWNLOADS_DIR / sanitize_filename(folder_label)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Browser-compat transcoding (HEVC -> H.264)
# ---------------------------------------------------------------------------
# Chromium-based browsers on Windows can't play HEVC without a paid Microsoft
# extension, and TikTok sometimes serves HEVC straight from iPhone uploads.
# Detect with ffprobe, transcode with ffmpeg (both come with Gyan.FFmpeg).
_BROWSER_INCOMPATIBLE_CODECS = {"hevc", "h265"}


def _ffprobe_codec(path: Path) -> str:
    """Return the codec_name of the first video stream (lowercased), or ''."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("ffprobe failed for %s: %s", path.name, e)
        return ""
    return (result.stdout or "").strip().lower()


def _transcode_to_h264(path: Path) -> Optional[Path]:
    """Transcode ``path`` to H.264 / AAC mp4 in place. Returns the new path
    on success (may differ from input if the input wasn't already .mp4),
    or None on failure."""
    final_path = path.with_suffix(".mp4")
    temp = path.parent / f"{path.stem}.transcoding.tmp.mp4"
    if temp.exists():
        try: temp.unlink()
        except OSError: pass

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(temp),
            ],
            check=True, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        log.error("ffmpeg failed for %s: %s", path.name, e)
        if temp.exists():
            try: temp.unlink()
            except OSError: pass
        return None

    try:
        if path.exists():
            path.unlink()
        temp.rename(final_path)
    except OSError as e:
        log.error("rename failed after transcode of %s: %s", path.name, e)
        return None
    return final_path


def ensure_browser_compatible(path: Path) -> Path:
    """If the file is HEVC, transcode to H.264 in place. Returns the
    (possibly new) path either way — never raises."""
    if not path or not path.exists():
        return path
    codec = _ffprobe_codec(path)
    if codec in _BROWSER_INCOMPATIBLE_CODECS:
        log.info("HEVC detected, transcoding to H.264: %s", path.name)
        before = path.stat().st_size
        new_path = _transcode_to_h264(path)
        if new_path:
            after = new_path.stat().st_size
            log.info("  transcoded: %.1f MB -> %.1f MB",
                     before / 1_048_576, after / 1_048_576)
            return new_path
    return path


def trim_to(path: Path, start: float, end: float) -> tuple[bool, str]:
    """Re-encode ``path`` in place to keep only the ``[start, end]`` (seconds)
    range. Re-encodes (rather than stream-copy) for frame-accurate cuts.
    Returns (ok, message)."""
    if not path.exists():
        return False, "file does not exist"
    if start < 0 or end <= start:
        return False, f"invalid range [{start}, {end}]"

    temp = path.parent / f"{path.stem}.trimming.tmp.mp4"
    if temp.exists():
        try: temp.unlink()
        except OSError: pass

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(path),
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(temp),
            ],
            check=True, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        if temp.exists():
            try: temp.unlink()
            except OSError: pass
        return False, f"ffmpeg failed: {e}"

    try:
        path.unlink()
        temp.rename(path)
    except OSError as e:
        return False, f"rename failed: {e}"
    return True, "ok"


def existing_file_for(folder_label: str, video_id: str) -> Optional[Path]:
    """Look for an already-downloaded file under any of the status roots
    (downloads/, approved/, rejected/, deleted/). The deleted/ folder holds
    JPEG thumbnails — those count for the scraper's disk-heal flow but
    NOT for download-time dedup (see existing_video_for)."""
    safe = sanitize_filename(folder_label)
    roots = [config.DOWNLOADS_DIR, config.APPROVED_DIR, config.REJECTED_DIR,
             config.DELETED_DIR]
    for root in roots:
        folder = root / safe
        if not folder.exists():
            continue
        for p in folder.glob(f"*{video_id}.*"):
            if p.is_file() and not p.name.endswith(".part"):
                return p
    return None


def existing_video_for(folder_label: str, video_id: str) -> Optional[Path]:
    """Like existing_file_for, but skips thumbnail JPEGs. Used by
    download_video() so a leftover thumbnail in deleted/ doesn't make the
    downloader think a real video file is already on disk — which would
    short-circuit the download AND the HEVC→H.264 transcode."""
    safe = sanitize_filename(folder_label)
    roots = [config.DOWNLOADS_DIR, config.APPROVED_DIR, config.REJECTED_DIR,
             config.DELETED_DIR]
    for root in roots:
        folder = root / safe
        if not folder.exists():
            continue
        for p in folder.glob(f"*{video_id}.*"):
            if not p.is_file() or p.name.endswith(".part"):
                continue
            if p.suffix.lower() in (".jpg", ".jpeg"):
                continue
            return p
    return None


def download_video(
    url: str,
    folder_label: str,
    *,
    video_id: Optional[str] = None,
    max_height: Optional[int] = None,
    quiet: bool = False,
) -> tuple[Optional[Path], dict]:
    """Download via yt-dlp. Returns (path, info_dict). path is None if the
    download failed for any reason.

    If ``video_id`` is provided we short-circuit when the file already exists
    (saving a yt-dlp extraction call). Otherwise we let yt-dlp resolve it.
    """
    if not url:
        raise ValueError("url is required")
    if not folder_label:
        raise ValueError("folder_label is required")

    if video_id:
        existing = existing_video_for(folder_label, video_id)
        if existing:
            log.info("already on disk, skipping: %s", existing)
            return existing, {"id": video_id, "_already_on_disk": True}

    height = int(max_height) if max_height else int(config.MAX_RESOLUTION)
    folder = _folder_for(folder_label)

    ydl_opts = {
        # Filename prefixed with upload date (YYYYMMDD) so the channel folder
        # sorts chronologically by name. yt-dlp substitutes "NA" if the field
        # is missing, which sorts last.
        "outtmpl": str(folder / "%(upload_date)s_%(id)s.%(ext)s"),
        "format": (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": quiet,
        "retries": 3,
        "fragment_retries": 3,
        "cachedir": False,
        "ignoreerrors": False,
        "socket_timeout": 30,  # bail on a hung TikTok server instead of hanging forever
    }
    cookies_browser = getattr(config, "TIKTOK_COOKIES_FROM_BROWSER", None)
    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

    log.info("downloading %s", url)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # match_filter rejection: yt-dlp returns the info dict but doesn't write a
    # file. We detect by checking whether the file actually exists.
    if not info:
        # Generic fail
        return None, {}
    final_id = info.get("id") or video_id or ""
    if not final_id:
        raise DownloadError(f"yt-dlp returned no id for {url}")

    final = existing_video_for(folder_label, final_id)
    if final and final.exists():
        # Transcode HEVC to H.264 so Chrome/Edge/Firefox on Windows can play it.
        final = ensure_browser_compatible(final)
        log.info("saved %s (%.1f MB)", final, final.stat().st_size / 1_048_576)
        return final, info

    log.warning("download finished but no file found for %s in %s", final_id, folder)
    return None, info


# ---------------------------------------------------------------------------
# Standalone test: `python downloader.py <url> [folder_label]`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: python downloader.py <url> [folder_label]"
        )

    url = sys.argv[1]
    folder_label = sys.argv[2] if len(sys.argv) >= 3 else "_test_"

    path, info = download_video(url, folder_label)
    if path:
        size_mb = path.stat().st_size / 1_048_576
        print(f"\nOK — saved to: {path}")
        print(f"     size:     {size_mb:.2f} MB")
    else:
        print("\nFiltered or failed (see log above).")
