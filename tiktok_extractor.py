"""TikTok user-feed extractor via yt-dlp.

Public surface:
    list_user_videos(handle, max_count=0) -> UserPull | None

Uses yt-dlp's flat-playlist mode to list all videos on a user's profile
without downloading. The listing itself usually includes duration, so we
can filter out anything outside config's window before queueing downloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import config

log = logging.getLogger(__name__)


def _date_from_video_id(video_id: str) -> str:
    """Decode upload date (YYYY-MM-DD) from a TikTok video ID. TikTok IDs are
    snowflake-style: the upper 32 bits are a Unix timestamp in seconds, so
    we get the upload date without any network call. Returns "" if the ID
    isn't a number or the timestamp is out of plausible range."""
    try:
        ts = int(video_id) >> 32
    except (TypeError, ValueError):
        return ""
    # TikTok launched mid-2017. Anything before 2015 or after 2096 is bogus.
    if ts < 1_420_000_000 or ts > 4_000_000_000:
        return ""
    return datetime.utcfromtimestamp(ts).date().isoformat()


@dataclass
class TikTokVideo:
    video_id: str
    url: str
    title: str = ""
    duration_seconds: Optional[int] = None
    view_count: int = 0
    upload_date: str = ""
    source_user: str = ""


@dataclass
class UserPull:
    handle: str
    user_url: str
    videos: list[TikTokVideo]
    raw_count: int = 0


def _ydl_base_opts() -> dict:
    """Common options. Wires browser cookies if configured."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,  # don't hang forever if TikTok is slow / blocking
    }
    cookies_browser = getattr(config, "TIKTOK_COOKIES_FROM_BROWSER", None)
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def list_user_videos(handle: str, max_count: int = 0) -> Optional[UserPull]:
    """List videos from a TikTok user feed.

    ``max_count``:
        0   → pull all available videos
        N>0 → only the first N from the feed (newest first)

    Returns None on extraction failure (TikTok blocked, user doesn't exist,
    network error, etc.).
    """
    h = (handle or "").strip().lstrip("@")
    if not h:
        return None
    user_url = f"https://www.tiktok.com/@{h}"
    source_user = f"@{h}"

    opts = _ydl_base_opts()
    opts["extract_flat"] = "in_playlist"
    if max_count > 0:
        opts["playlist_items"] = f"1-{max_count}"

    log.info("listing TikTok user %s (%s)",
             source_user, "all" if max_count == 0 else f"first {max_count}")

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(user_url, download=False)
    except (DownloadError, Exception) as e:
        log.error("failed to list %s: %s: %s", source_user, type(e).__name__, e)
        return None

    if not info:
        log.warning("no info returned for %s", source_user)
        return None

    entries = info.get("entries") or []
    videos: list[TikTokVideo] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        # yt-dlp's flat-playlist gives the video URL under either "url"
        # or "webpage_url" depending on extractor; fall back to building.
        url = e.get("url") or e.get("webpage_url") or (
            f"https://www.tiktok.com/@{h}/video/{vid}" if vid else ""
        )
        if not vid or not url:
            continue
        videos.append(TikTokVideo(
            video_id=str(vid),
            url=url,
            title=(e.get("title") or "")[:200],
            duration_seconds=e.get("duration"),
            view_count=int(e.get("view_count") or 0),
            upload_date=_date_from_video_id(str(vid)),
            source_user=source_user,
        ))

    raw_count = len(videos)

    # Apply the date filter using the decoded upload date — costs nothing.
    min_date = getattr(config, "MIN_UPLOAD_DATE", None)
    if min_date:
        before = len(videos)
        videos = [v for v in videos if v.upload_date and v.upload_date >= min_date]
        dropped = before - len(videos)
        if dropped:
            log.info("  date filter (>= %s): kept %d, dropped %d older",
                     min_date, len(videos), dropped)

    log.info("  got %d videos from %s", len(videos), source_user)
    return UserPull(
        handle=source_user,
        user_url=user_url,
        videos=videos,
        raw_count=raw_count,
    )


# ---------------------------------------------------------------------------
# Standalone smoke test: `python tiktok_extractor.py [@handle]`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import csv
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    handle: Optional[str] = None
    if len(sys.argv) >= 2:
        handle = sys.argv[1]
    else:
        with open(config.USERS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                h = (row.get("handle") or "").strip()
                if h and not h.startswith("@example_") and "replace_me" not in h.lower():
                    handle = h
                    break
        if not handle:
            raise SystemExit(
                f"No usable handle in {config.USERS_CSV}. Pass one as an arg:\n"
                f"  python tiktok_extractor.py @somecreator"
            )

    # Limit to 20 for the smoke test so it doesn't take forever
    print(f"\nListing first 20 videos from {handle}...\n")
    pull = list_user_videos(handle, max_count=20)
    if pull is None:
        raise SystemExit(f"Could not list videos for {handle!r}")

    print(f"Got {len(pull.videos)} videos (after date filter "
          f">= {config.MIN_UPLOAD_DATE or 'none'}).\n")

    for v in pull.videos:
        dur = f"{v.duration_seconds}s" if v.duration_seconds is not None else "??s"
        print(f"  [{v.video_id}]  {v.upload_date or '????-??-??'}  {dur:>5}  {v.title[:80]}")
