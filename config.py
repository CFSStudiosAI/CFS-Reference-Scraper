"""Central configuration for the Dance Scraper (TikTok mode)."""

from datetime import date as _date
from pathlib import Path

from dotenv import load_dotenv

# --- Paths -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_ROOT / "input"
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"   # newly scraped, awaiting review
APPROVED_DIR = PROJECT_ROOT / "approved"     # 👍 in the UI
REJECTED_DIR = PROJECT_ROOT / "rejected"     # 👎 in the UI
DELETED_DIR  = PROJECT_ROOT / "deleted"      # video gone, only thumbnail kept
LOGS_DIR = PROJECT_ROOT / "logs"

USERS_CSV = INPUT_DIR / "tiktok_users.csv"
DATABASE_PATH = PROJECT_ROOT / "library.db"

# --- Secrets / cookies -------------------------------------------------------
# Load .env at import so any platform-specific API keys land in os.getenv()
# for future modules. Currently nothing here uses .env, but kept wired up for
# eventual YouTube / Instagram extensions.
load_dotenv(PROJECT_ROOT / ".env")

# When TikTok blocks anonymous scraping for a user, set this to your browser
# name to use that browser's logged-in cookies. Options yt-dlp accepts:
#   "firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium"
# Leave as None to scrape anonymously (works for most public accounts).
TIKTOK_COOKIES_FROM_BROWSER: str | None = None

# --- Tunables ---------------------------------------------------------------
# Cap on how many newest videos to download per user per run. Set to 0 to
# pull every video on the account. Combined with DB dedup, the first run
# grabs the back-catalog and later runs only fetch new uploads.
VIDEOS_PER_USER = 0

# Only download videos uploaded on or after this date (YYYY-MM-DD).
# Defaults to January 1 of the current year — auto-updates each new year.
# TikTok video IDs encode their upload timestamp, so this filter runs
# against the listing for free (no extra network calls per video).
# Set to None to disable the date filter.
MIN_UPLOAD_DATE: str | None = f"{_date.today().year}-01-01"

# yt-dlp will pick the best available video <= this height.
MAX_RESOLUTION = "720"

# Number of videos shown per page in the browse UI.
PAGE_SIZE = 10
