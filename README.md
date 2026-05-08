# CFSStudios.AI TT Scraper

Local-first TikTok scraping + review tool. Curate a list of creators, the scraper pulls their uploads into an organized library, and a browser UI lets you triage them with approve / reject / delete / restore states, trim-on-approve, and live search.

Windows-only. No cloud, no accounts. Everything stays on your machine.

Repo: <https://github.com/CFSStudiosAI/CFS-Reference-Scraper>

## Setup

1. Install **Python 3.10+** from <https://www.python.org/downloads/> (tick "Add Python to PATH").
2. Install **ffmpeg**:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Reopen any open shells so the new PATH is picked up.
3. Double-click **`start.bat`**. First run creates the venv, installs dependencies, force-updates yt-dlp, seeds `input/tiktok_users.csv` from the example template, and launches the browser at <http://127.0.0.1:5000/>.

## Reviewing

Pages of 10 cards across four tabs (Pending / Approved / Rejected / Deleted), all auto-playing muted+looped. Click a card or use keyboard shortcuts in the lightbox:

| Key | Action |
|---|---|
| `A` | Approve. If the trim sliders are moved, re-encodes to the selected range first. |
| `R` | Reject. |
| `→` / `←` | Next / previous card (crosses page boundaries). |
| `Esc` | Close lightbox. |

The trim sliders are always visible; dragging either scrubs the video so you see exactly which frame you're cutting to. The Approve button label flips to **Approve & Trim** when sliders are moved.

## Bulk actions

- **Delete Rejected** (button on the Rejected tab) — replaces each rejected video file with a thumbnail JPEG and moves the row to Deleted. The DB record stays so the scraper never re-downloads it.
- **Restore** (button on each Deleted card) — re-downloads the original from TikTok, flips the row back to Pending.
- **Live search** — filters the current tab by description (TikTok caption) text, debounced as you type.
- **Edit Creators** — add, rename, or delete creators. Renaming migrates all existing files for that creator into the new folder name across every status root and removes orphan folders. Deleting offers two modes: keep approved videos, or wipe everything for that creator.

## Layout

| Path | Role |
|---|---|
| `input/tiktok_users.csv` | Tracked creators (`handle,name`) |
| `downloads/<creator>/` | Pending — newly scraped, awaiting verdict |
| `approved/<creator>/` | 👍 reference library |
| `rejected/<creator>/` | 👎 retrievable discards |
| `deleted/<creator>/` | Thumbnail JPEGs only — file deleted, never re-downloaded |
| `library.db` | SQLite — every video keyed by TikTok's permanent `video_id` |
| `logs/scraper_<date>.log` | Background scraper output |

The DB is keyed by TikTok's `video_id`. **Once a video is in the DB in any state, it never re-downloads** — even after deletion, even if you wipe the DB (the disk-heal logic finds existing files and rebuilds the row from where they live).

## Tunables (`config.py`)

| Setting | Default | What it does |
|---|---|---|
| `VIDEOS_PER_USER` | `0` | Cap per creator per scrape. `0` = unlimited. |
| `MIN_UPLOAD_DATE` | `Jan 1 of current year` | YYYY-MM-DD floor. Older uploads are dropped without hitting the network — TikTok video IDs are snowflake-encoded with the upload timestamp. Set to `None` to disable. |
| `MAX_RESOLUTION` | `"720"` | yt-dlp picks best stream up to this height. |
| `PAGE_SIZE` | `10` | Cards per page in the UI. |
| `TIKTOK_COOKIES_FROM_BROWSER` | `None` | Set to `"firefox"`, `"chrome"`, `"edge"`, etc. if TikTok blocks anonymous scraping. yt-dlp will then read that browser's logged-in cookies. |

## Updating

If you cloned with git:

```powershell
git pull
```

inside the project folder. Then run `start.bat` as normal — it auto-handles any new dependencies.

If you downloaded the ZIP, redownload it from the repo and copy `library.db`, `input/tiktok_users.csv`, and the four media folders (`downloads/`, `approved/`, `rejected/`, `deleted/`) into the new copy.



## Troubleshooting

- **TikTok extraction broken** — almost always means yt-dlp needs to update for TikTok's latest internal API change. `start.bat` does this on every launch, so just re-run it. If problems persist, check <https://github.com/yt-dlp/yt-dlp/issues>.
- **Black thumbnails / videos won't play** — usually missing or stale ffmpeg. Run `ffprobe -version` in a fresh shell to verify. (HEVC clips from iPhones are auto-transcoded to H.264 at download time, so they should never get to a "won't play" state on their own.)
- **TikTok blocking certain creators** — set `TIKTOK_COOKIES_FROM_BROWSER` in `config.py` to your browser name, log into TikTok in that browser.

## License

See `LICENSE`. Personal use only — no redistribution, no resale, no modification.
