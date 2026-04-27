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


POSITIVE_NEWS_MAX_SESSIONS = 3
NEGATIVE_NEWS_MAX_SESSIONS = 5

NEWS_SCOPE_LABELS = {
    "company": "خبر شركة",
    "sector": "خبر قطاع",
    "market": "سياق سوق عام",
    "opinion": "مقال رأي",
    "neutral": "خبر محايد",
    "unrelated": "غير ذي صلة",
}

