# CFSStudios.AI TT Scraper

Local-first TikTok scraping + review tool for animation reference work. Curate a list of creators, the scraper pulls their uploads into an organized library, and a browser UI lets you triage them with approve / reject / delete / restore states, trim-on-approve, and live search.

Windows-only. No cloud. No accounts. Everything stays on your machine.

## Setup

1. Install **Python 3.10+** from <https://www.python.org/downloads/>. Tick "Add Python to PATH" in the installer.

2. Install **ffmpeg** (required for downloading and transcoding):
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Close and reopen any open shells so the new PATH takes effect.

3. **Double-click `start.bat`** in this folder.

   On first run it'll:
   - Create a `.venv` virtual environment.
   - Install the three Python dependencies (`yt-dlp`, `python-dotenv`, `Flask`).
   - Force-update `yt-dlp` to the latest (because TikTok rotates internal APIs every few weeks).
   - Seed `input/tiktok_users.csv` from the example template if you don't have one yet.
   - Launch the local server at <http://127.0.0.1:5000/> and open it in your browser.
   - Kick off a background scrape.

   Subsequent runs skip the venv setup and go straight to launching.

## Adding creators

Open the **Edit Creators** button in the top-right. Add `@handle` rows, optionally with a friendly display name. Or edit `input/tiktok_users.csv` directly (`handle,name` per row).

When you save a name change in the UI, all existing files for that creator migrate to the new folder name across `downloads/`, `approved/`, `rejected/`, and `deleted/` — no orphaned folders.

## Reviewing

Every tab (Pending / Approved / Rejected / Deleted) shows 10 cards per page, all auto-playing muted+looped.

Click any card or hit a keyboard shortcut to open the lightbox:

| Key | Action |
|---|---|
| `A` | Approve (re-encodes if you moved the trim sliders, then moves to `approved/`) |
| `R` | Reject (moves to `rejected/`) |
| `→` / `←` | Next / previous card (crosses page boundaries) |
| `Esc` | Close lightbox |

Lightbox extras:
- **Trim sliders** are always visible. Drag start/end and the video scrubs to that frame so you see exactly where the cut lands. The button text flips to "Approve & Trim" when sliders are moved; on click the file is re-encoded with ffmpeg before being moved to `approved/`.
- The "CFSStudios.AI" header link opens [cfsstudios.ai](https://www.cfsstudios.ai/) in a new tab.

## Bulk actions

- **Delete Rejected** (button on the Rejected tab) — re-encodes each rejected video down to a single thumbnail JPEG and moves the row to the Deleted state. The original file is gone but the thumbnail (and DB record) stays so you remember you saw it.
- **Restore** (button on each Deleted card) — re-downloads the original from TikTok and flips the row back to Pending.
- **Live search** — type in the search box. Filters the current tab by description (TikTok caption) text, debounced live as you type.

## How it stays organized

| Folder | What lives there |
|---|---|
| `downloads/<creator>/` | Newly scraped, awaiting your verdict |
| `approved/<creator>/` | 👍 — your reference library |
| `rejected/<creator>/` | 👎 — discarded but retrievable |
| `deleted/<creator>/` | Just thumbnails (`.jpg`) — video file gone, but never re-downloaded |
| `library.db` | SQLite — every video's `video_id`, status, file path, metadata |
| `logs/scraper_<date>.log` | Background scraper output |

The DB is keyed by TikTok's permanent `video_id`. **Once a video is in the DB in any state, it never re-downloads** — even after deletion, even if you wipe the DB and run again (the disk-heal logic finds existing files and rebuilds the row from where it lives).

## Tunables (`config.py`)

| Setting | Default | What it does |
|---|---|---|
| `VIDEOS_PER_USER` | `0` | Cap on videos pulled per creator per scrape. `0` means "all available." |
| `MIN_UPLOAD_DATE` | `Jan 1 of current year` | YYYY-MM-DD floor; older uploads are dropped without ever hitting the network (TikTok IDs are snowflake-encoded, so the date comes for free). Set to `None` to disable. |
| `MAX_RESOLUTION` | `"720"` | yt-dlp picks best available stream up to this height. |
| `PAGE_SIZE` | `10` | Cards per page in the UI. |
| `TIKTOK_COOKIES_FROM_BROWSER` | `None` | Set to `"firefox"`, `"chrome"`, `"edge"`, etc. if TikTok blocks anonymous scraping. yt-dlp will then read your logged-in TikTok cookies from that browser. |

## Maintenance

- **`fix_hevc.bat`** — one-shot scan of `downloads/`, `approved/`, `rejected/`, and `deleted/`; transcodes any HEVC (iPhone-shot) videos to H.264 in place. Idempotent — already-H.264 files are detected and skipped.
- **TikTok extraction broken?** TikTok rotates their internal API every few weeks. The fix is almost always to update yt-dlp, which `start.bat` already does on every launch. If problems persist, check the GitHub issues at <https://github.com/yt-dlp/yt-dlp/issues>.
- **All your videos look black or won't play?** Almost always a missing or stale ffmpeg install. Verify with `ffprobe -version` in a fresh shell.

## License

See `LICENSE`. Personal use only — no redistribution, no resale, no modification.
