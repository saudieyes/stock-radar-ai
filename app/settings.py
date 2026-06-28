import os
import hashlib
import requests
from pathlib import Path
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent.parent if "__file__" in globals() else Path.cwd()
APP_DATA_DIR_ENV = str(os.getenv("APP_DATA_DIR", "") or "").strip()
if APP_DATA_DIR_ENV:
    DATA_DIR = Path(APP_DATA_DIR_ENV).expanduser()
elif Path("/data").exists():
    DATA_DIR = Path("/data")
else:
    DATA_DIR = BASE_DIR / "app_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

APP_AUTH_USERNAME = str(os.getenv("APP_BASIC_AUTH_USERNAME", "") or "").strip()
APP_AUTH_PASSWORD = str(os.getenv("APP_BASIC_AUTH_PASSWORD", "") or "").strip()
APP_AUTH_ENABLED = bool(APP_AUTH_USERNAME and APP_AUTH_PASSWORD)
APP_AUTH_SESSION_DAYS = int(float(os.getenv("APP_AUTH_SESSION_DAYS", "14") or 14))
APP_SESSION_SECRET = os.getenv("APP_SESSION_SECRET") or hashlib.sha256(
    f"{APP_AUTH_USERNAME}:{APP_AUTH_PASSWORD}:stock-radar".encode("utf-8")
).hexdigest()
APP_AUTH_COOKIE_NAME = "sr_auth"
AUTH_EXEMPT_PATHS = {"/health", "/login", "/logout", "/session"}

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

HTTP_SESSION = requests.Session()
HTTP_ADAPTER = HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=0)
HTTP_SESSION.mount("https://", HTTP_ADAPTER)
HTTP_SESSION.mount("http://", HTTP_ADAPTER)

HISTORY_CACHE = {}
REF_INFO_CACHE = {}
INTRADAY_CACHE = {}
SNAPSHOT_CACHE = {}
PERFORMANCE_REFRESH_CACHE = {}
CONTEXT_CACHE = {}

PERFORMANCE_FILE = str(DATA_DIR / "signal_performance.json")
MANUAL_WATCHLIST_FILE = str(DATA_DIR / "manual_watchlist.json")
PORTFOLIO_FILE = str(DATA_DIR / "portfolio_holdings.json")
MANUAL_SHARIA_EXCLUSIONS_FILE = str(DATA_DIR / "manual_sharia_exclusions.json")
MANUAL_SHARIA_APPROVALS_FILE = str(DATA_DIR / "manual_sharia_approvals.json")

SECTOR_ETF_MAP =  {
    "technology": "XLK",
    "information technology": "XLK",
    "semiconductors": "XLK",
    "semiconductor": "XLK",
    "software": "XLK",
    "hardware": "XLK",
    "electronics": "XLK",
    "electronic": "XLK",
    "computer": "XLK",
    "cybersecurity": "XLK",
    "cloud": "XLK",
    "ai": "XLK",
    "communication services": "XLC",
    "communication": "XLC",
    "telecom": "XLC",
    "internet": "XLC",
    "media": "XLC",
    "consumer cyclical": "XLY",
    "consumer discretionary": "XLY",
    "consumer": "XLY",
    "retail": "XLY",
    "apparel": "XLY",
    "restaurant": "XLY",
    "restaurants": "XLY",
    "travel": "XLY",
    "auto": "XLY",
    "automobile": "XLY",
    "consumer defensive": "XLP",
    "consumer staples": "XLP",
    "staples": "XLP",
    "food": "XLP",
    "beverage": "XLP",
    "grocery": "XLP",
    "household": "XLP",
    "healthcare": "XLV",
    "health care": "XLV",
    "biotech": "XLV",
    "biotechnology": "XLV",
    "pharma": "XLV",
    "pharmaceutical": "XLV",
    "drug": "XLV",
    "medical": "XLV",
    "diagnostic": "XLV",
    "hospital": "XLV",
    "industrials": "XLI",
    "industrial": "XLI",
    "aerospace": "XLI",
    "defense": "XLI",
    "transport": "XLI",
    "transportation": "XLI",
    "airline": "XLI",
    "rail": "XLI",
    "machinery": "XLI",
    "energy": "XLE",
    "oil": "XLE",
    "gas": "XLE",
    "drilling": "XLE",
    "exploration": "XLE",
    "utilities": "XLU",
    "utility": "XLU",
    "water": "XLU",
    "real estate": "XLRE",
    "reit": "XLRE",
    "property": "XLRE",
    "materials": "XLB",
    "basic materials": "XLB",
    "chemical": "XLB",
    "chemicals": "XLB",
    "mining": "XLB",
    "metals": "XLB",
    "steel": "XLB",
    "financial services": "XLF",
    "financial": "XLF",
    "bank": "XLF",
    "banks": "XLF",
    "insurance": "XLF",
    "capital markets": "XLF",
}

INTRADAY_CACHE_TTL_OPEN = 12
INTRADAY_CACHE_TTL_CLOSED = 60
SNAPSHOT_CACHE_TTL_OPEN = 8
SNAPSHOT_CACHE_TTL_EXTENDED = 15
SNAPSHOT_CACHE_TTL_CLOSED = 120

HARAM_SECTORS = {"financial services", "banks", "insurance"}

HARAM_INDUSTRY_KEYWORDS = [
    "bank", "banks", "insurance", "tobacco", "alcohol",
    "gambling", "casino", "betting", "credit services",
    "mortgage", "reit mortgage", "asset management", "capital markets",
]

LOW_PRICE_HARD_BLOCK = 2.0
LOW_PRICE_WARNING = 3.0


# Sharia Filter V2 thresholds (configurable via Railway variables).
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return float(default)

SHARIA_MAX_DEBT_TO_ASSETS = _env_float("SHARIA_MAX_DEBT_TO_ASSETS", 0.33)
SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE = _env_float("SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE", 0.05)
SHARIA_MAX_IMPERMISSIBLE_REVENUE_RATIO = _env_float("SHARIA_MAX_IMPERMISSIBLE_REVENUE_RATIO", 0.05)
SHARIA_GRAY_CASH_TO_ASSETS = _env_float("SHARIA_GRAY_CASH_TO_ASSETS", 0.33)
SHARIA_BLOCK_GRAY_IN_SOURCE = str(os.getenv("SHARIA_BLOCK_GRAY_IN_SOURCE", "false") or "false").lower() in {"1", "true", "yes", "on"}

# V2W19a: SEC XBRL is the primary Sharia financial evidence source.
# The old SimFin/CSV files remain bundled as historical reference, but by default
# they no longer grant a clean Sharia result when SEC primary mode is enabled.
SHARIA_SEC_PRIMARY_ENABLED = str(os.getenv("SHARIA_SEC_PRIMARY_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
SHARIA_LEGACY_FINANCIALS_ENABLED = str(os.getenv("SHARIA_LEGACY_FINANCIALS_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
# V2W19c: SEC formula failures are priority warnings by default, not final hard blocks.
# Set true only if you later want to restore strict financial blocking.
SHARIA_SEC_FINANCIAL_WARNING_BLOCKS = str(os.getenv("SHARIA_SEC_FINANCIAL_WARNING_BLOCKS", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
SEC_SHARIA_DATA_DIR = Path(os.getenv("SEC_SHARIA_DATA_DIR", str(DATA_DIR / "sec")) or str(DATA_DIR / "sec"))
SEC_COMPANYFACTS_ZIP = str(os.getenv("SEC_COMPANYFACTS_ZIP", str(SEC_SHARIA_DATA_DIR / "companyfacts.zip")) or str(SEC_SHARIA_DATA_DIR / "companyfacts.zip"))
SEC_TICKERS_EXCHANGE_JSON = str(os.getenv("SEC_TICKERS_EXCHANGE_JSON", str(SEC_SHARIA_DATA_DIR / "company_tickers_exchange.json")) or str(SEC_SHARIA_DATA_DIR / "company_tickers_exchange.json"))
SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS = int(_env_float("SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS", 540.0))

# GitHub sync for durable app-generated data (manual Sharia exclusions, later archives).
GITHUB_SYNC_TOKEN = str(os.getenv("GITHUB_SYNC_TOKEN", "") or "").strip()
GITHUB_SYNC_REPO = str(os.getenv("GITHUB_SYNC_REPO", "") or "").strip()  # owner/repo
GITHUB_SYNC_BRANCH = str(os.getenv("GITHUB_SYNC_BRANCH", "main") or "main").strip()
GITHUB_SYNC_ENABLED = bool(GITHUB_SYNC_TOKEN and GITHUB_SYNC_REPO)
GITHUB_SYNC_MANUAL_SHARIA_PATH = str(os.getenv("GITHUB_SYNC_MANUAL_SHARIA_PATH", "app_data/manual_sharia_exclusions.json") or "app_data/manual_sharia_exclusions.json").strip()
GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH = str(os.getenv("GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH", "app_data/manual_sharia_approvals.json") or "app_data/manual_sharia_approvals.json").strip()
GITHUB_SYNC_TIMEOUT_SEC = _env_float("GITHUB_SYNC_TIMEOUT_SEC", 12.0)
GITHUB_SYNC_PULL_TTL_SEC = int(_env_float("GITHUB_SYNC_PULL_TTL_SEC", 900.0))

# Source refill controls. These do not cache live price data; they only control how wide
# the source looks before the Sharia prefilter removes/replaces symbols.
SHARIA_SOURCE_REFILL_MULTIPLIER = _env_float("SHARIA_SOURCE_REFILL_MULTIPLIER", 3.2)
SHARIA_SOURCE_REFILL_MIN_RESERVE = int(_env_float("SHARIA_SOURCE_REFILL_MIN_RESERVE", 620.0))
SHARIA_SOURCE_REFILL_MAX_RESERVE = int(_env_float("SHARIA_SOURCE_REFILL_MAX_RESERVE", 700.0))
SHARIA_SOURCE_GRAY_MAX_RATIO = _env_float("SHARIA_SOURCE_GRAY_MAX_RATIO", 0.24)
SHARIA_SOURCE_GRAY_MIN_HARD_CAP = int(_env_float("SHARIA_SOURCE_GRAY_MIN_HARD_CAP", 18.0))
SHARIA_SOURCE_GRAY_SOFT_CAP = int(_env_float("SHARIA_SOURCE_GRAY_SOFT_CAP", 48.0))

# Fix16 controls: speed up scan execution without caching live price/quote data.
# Keep universe broad by default, but allow Railway override if needed.
SCAN_UNIVERSE_TARGET = int(_env_float("SCAN_UNIVERSE_TARGET", 190.0))
SCAN_MAX_WORKERS = int(_env_float("SCAN_MAX_WORKERS", 16.0))



def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}

# Durable runtime storage / first-run setup / live data controls.
USE_SQLITE_STORAGE = _env_bool("USE_SQLITE_STORAGE", True)
FIRST_RUN_SETUP_ENABLED = _env_bool("FIRST_RUN_SETUP_ENABLED", True)
NEWS_SCORE_ENABLED = _env_bool("NEWS_SCORE_ENABLED", False)
LIVE_QUOTES_ENABLED = _env_bool("LIVE_QUOTES_ENABLED", True)
FMP_API_KEY = str(os.getenv("FMP_API_KEY", "") or "").strip()
FMP_WEBSOCKET_ENABLED = _env_bool("FMP_WEBSOCKET_ENABLED", False)

# AI news classifier (Claude / Anthropic). The model classifies news only;
# the trading engine still decides whether catalyst points are allowed.
ANTHROPIC_API_KEY = str(os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
AI_NEWS_PROVIDER = str(os.getenv("AI_NEWS_PROVIDER", "claude") or "claude").strip().lower()
AI_NEWS_MODEL = str(os.getenv("AI_NEWS_MODEL", "claude-haiku-4-5-20251001") or "claude-haiku-4-5-20251001").strip()
AI_NEWS_ENABLED = _env_bool("AI_NEWS_ENABLED", False) and bool(ANTHROPIC_API_KEY)
AI_NEWS_TIMEOUT_SEC = _env_float("AI_NEWS_TIMEOUT_SEC", 8.0)
AI_NEWS_CACHE_TTL_SECONDS = int(_env_float("AI_NEWS_CACHE_TTL_SECONDS", 21600.0))
AI_NEWS_MIN_CONFIDENCE = int(_env_float("AI_NEWS_MIN_CONFIDENCE", 70.0))
AI_NEWS_MAX_CLASSIFY_PER_SYMBOL = int(_env_float("AI_NEWS_MAX_CLASSIFY_PER_SYMBOL", 1.0))

POSITIVE_NEWS_MAX_SESSIONS = 3
NEGATIVE_NEWS_MAX_SESSIONS = 5


# Weekly archive / retention controls. Safe by default: archive can run on demand; pruning requires explicit env/param.
GITHUB_WEEKLY_ARCHIVE_PATH = str(os.getenv("GITHUB_WEEKLY_ARCHIVE_PATH", "app_data/weekly_tracking_archive") or "app_data/weekly_tracking_archive").strip().strip("/")
WEEKLY_ARCHIVE_ENABLED = _env_bool("WEEKLY_ARCHIVE_ENABLED", True)
WEEKLY_ARCHIVE_TOKEN = str(os.getenv("WEEKLY_ARCHIVE_TOKEN", "") or "").strip()
WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS = _env_bool("WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS", False)
WEEKLY_ARCHIVE_RETENTION_WEEKS = int(_env_float("WEEKLY_ARCHIVE_RETENTION_WEEKS", 2.0))

NEWS_SCOPE_LABELS = {
    "company": "خبر شركة",
    "sector": "خبر قطاع",
    "market": "سياق سوق عام",
    "opinion": "مقال رأي",
    "neutral": "خبر محايد",
    "unrelated": "غير ذي صلة",
}


