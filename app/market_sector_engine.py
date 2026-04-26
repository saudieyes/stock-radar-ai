from .settings import CONTEXT_CACHE, SECTOR_ETF_MAP
from .utils import *
from .data_loader import COMPANIES_DATA, SECTOR_DATA
from .market_data import get_daily_bars, get_trend

SYMBOL_CONTEXT_FALLBACKS = {
    "AAPL": ("Technology", "Consumer Electronics / Electronic Computers"),
    "MSFT": ("Technology", "Software / Cloud"),
    "NVDA": ("Technology", "Semiconductors"),
    "AMD": ("Technology", "Semiconductors"),
    "AVGO": ("Technology", "Semiconductors"),
    "META": ("Communication Services", "Internet / Social Media"),
    "GOOGL": ("Communication Services", "Internet / Search / Cloud"),
    "GOOG": ("Communication Services", "Internet / Search / Cloud"),
    "AMZN": ("Consumer Discretionary", "Internet Retail / Cloud"),
    "TSLA": ("Consumer Discretionary", "Automobiles / EV"),
    "NFLX": ("Communication Services", "Streaming / Entertainment"),
    "DIS": ("Communication Services", "Media / Entertainment"),
    "IHRT": ("Communication Services", "Broadcasting / Radio / Media"),
}


def _context_closes_from_bars(bars: list[dict]) -> list[float]:
    closes = []
    for row in bars or []:
        try:
            c = to_float((row or {}).get("c", 0))
            if c > 0:
                closes.append(float(c))
        except Exception:
            continue
    return closes


def _context_return_pct(bars: list[dict], lookback: int = 20) -> float:
    closes = _context_closes_from_bars(bars)
    if len(closes) < lookback + 1:
        return 0.0
    start = float(closes[-(lookback + 1)] or 0)
    end = float(closes[-1] or 0)
    if start <= 0 or end <= 0:
        return 0.0
    return ((end - start) / start) * 100.0


def _infer_context_from_text(sector: str = "", industry: str = "") -> tuple[str, str]:
    raw = " ".join([str(sector or ""), str(industry or "")]).strip()
    t = raw.lower()
    if not t:
        return "", ""
    if any(x in t for x in [
        "technology", "information technology", "electronic computer", "electronic computers",
        "computer", "consumer electronics", "software", "semiconductor", "semiconductors",
        "chip", "data processing", "cloud", "computer programming", "hardware",
        "communications equipment", "cyber", "artificial intelligence"
    ]):
        return "Technology", raw
    if any(x in t for x in [
        "communication services", "media", "broadcast", "broadcasting", "radio", "internet",
        "social media", "search", "streaming", "telecommunications", "telecom", "wireless", "entertainment"
    ]):
        return "Communication Services", raw
    if any(x in t for x in ["retail", "restaurant", "automobile", "auto", "apparel", "travel", "hotel", "leisure", "ev"]):
        return "Consumer Discretionary", raw
    if any(x in t for x in ["food", "beverage", "grocery", "household", "tobacco", "personal care"]):
        return "Consumer Staples", raw
    if any(x in t for x in ["pharmaceutical", "biotech", "medical", "health", "drug", "diagnostic", "hospital"]):
        return "Healthcare", raw
    if any(x in t for x in ["bank", "insurance", "financial", "capital markets", "credit", "asset management"]):
        return "Financials", raw
    if any(x in t for x in ["oil", "gas", "energy", "drilling", "exploration", "petroleum"]):
        return "Energy", raw
    if any(x in t for x in ["aerospace", "defense", "machinery", "industrial", "transport", "airline", "logistics"]):
        return "Industrials", raw
    if any(x in t for x in ["chemical", "mining", "metals", "steel", "materials", "paper", "packaging"]):
        return "Materials", raw
    if any(x in t for x in ["utility", "electric", "water", "natural gas distribution"]):
        return "Utilities", raw
    if any(x in t for x in ["reit", "real estate", "property"]):
        return "Real Estate", raw
    return "", raw


def _normalize_context_inputs(symbol: str, sector: str, industry: str) -> tuple[str, str]:
    symbol = str(symbol or "").upper().strip()
    sector = str(sector or "").strip()
    industry = str(industry or "").strip()

    fb = SYMBOL_CONTEXT_FALLBACKS.get(symbol)
    if fb:
        if not sector or sector.lower() in {"unknown", "none", "n/a", "غير متوفر", "غير واضح"}:
            sector = fb[0]
        if not industry or industry.lower() in {"unknown", "none", "n/a", "غير متوفر", "غير واضح"}:
            industry = fb[1]

    inferred_sector, inferred_industry = _infer_context_from_text(sector, industry)
    if inferred_sector and (not sector or sector.lower() in {"unknown", "none", "n/a", "غير متوفر", "غير واضح"}):
        sector = inferred_sector
    if inferred_industry and not industry:
        industry = inferred_industry
    return sector, industry


def _context_benchmark_for_sector(sector: str, industry: str = "") -> str:
    s = str(sector or "").lower().strip()
    i = str(industry or "").lower().strip()
    combined = f"{s} {i}"
    techish = [
        "technology", "information technology", "software", "semiconductor", "chip", "ai", "cloud",
        "computer", "electronic", "hardware", "data processing", "cyber", "internet", "search", "social media"
    ]
    if any(x in combined for x in techish):
        return "QQQ"
    return "SPY"


def _context_sector_etf(sector: str, industry: str = "") -> str:
    s = str(sector or "").lower().strip()
    i = str(industry or "").lower().strip()
    combined = " ".join(x for x in [s, i] if x).strip()
    if not combined:
        return ""

    for key, value in SECTOR_ETF_MAP.items():
        if str(key).lower() in combined:
            return value

    if any(x in combined for x in [
        "technology", "information technology", "semiconductor", "chip", "software", "cloud", "cyber",
        "computer", "electronic computer", "consumer electronics", "hardware", "data processing",
        "communications equipment"
    ]):
        return "XLK"
    if any(x in combined for x in ["communication services", "internet", "streaming", "social media", "telecom", "wireless", "search", "media", "broadcast", "radio", "entertainment"]):
        return "XLC"
    if any(x in combined for x in ["consumer discretionary", "retail", "apparel", "restaurant", "travel", "auto", "automobile", "ev"]):
        return "XLY"
    if any(x in combined for x in ["consumer staples", "food", "beverage", "grocery", "household"]):
        return "XLP"
    if any(x in combined for x in ["healthcare", "health care", "biotech", "pharma", "drug", "medical", "diagnostic", "hospital"]):
        return "XLV"
    if any(x in combined for x in ["industrials", "aerospace", "defense", "transport", "airline", "machinery", "logistics"]):
        return "XLI"
    if any(x in combined for x in ["energy", "oil", "gas", "drilling", "exploration"]):
        return "XLE"
    if any(x in combined for x in ["materials", "chemical", "mining", "metals", "steel", "paper", "packaging"]):
        return "XLB"
    if any(x in combined for x in ["utilities", "utility", "water", "electric"]):
        return "XLU"
    if any(x in combined for x in ["real estate", "reit", "property"]):
        return "XLRE"
    if any(x in combined for x in ["financials", "bank", "insurance", "capital markets", "financial"]):
        return "XLF"
    return ""


def _context_trend_points(trend: str) -> int:
    trend = str(trend or "")
    if trend == "صاعد قوي":
        return 8
    if trend == "صاعد":
        return 4
    if trend == "متذبذب":
        return 0
    if trend == "هابط":
        return -6
    return 0


def _context_relative_points(relative_pct: float) -> int:
    rel = float(relative_pct or 0)
    if rel >= 8:
        return 4
    if rel >= 3:
        return 2
    if rel <= -8:
        return -4
    if rel <= -3:
        return -2
    return 0


def _context_support_label(score: float) -> str:
    score = float(score or 0)
    if score >= 8:
        return "داعم قوي"
    if score >= 3:
        return "داعم"
    if score <= -8:
        return "ضاغط قوي"
    if score <= -3:
        return "ضاغط"
    return "محايد"


def _context_alignment_label(total_score: float) -> str:
    score = float(total_score or 0)
    if score >= 12:
        return "يدعم بقوة"
    if score >= 5:
        return "يدعم"
    if score <= -12:
        return "معاكس بقوة"
    if score <= -5:
        return "معاكس"
    return "متوازن"


def _context_alignment_detail(market_label: str, sector_label: str, benchmark_symbol: str, sector_symbol: str, rel_market: float, rel_sector: float) -> str:
    parts = [f"المؤشر المرجعي {benchmark_symbol}: {market_label}"]
    if sector_symbol:
        parts.append(f"ETF القطاع {sector_symbol}: {sector_label}")
    else:
        parts.append("ETF القطاع غير متوفر: هذه الفرصة بثقة أقل قليلًا لأن السياق القطاعي غير مكتمل")
    if abs(float(rel_market or 0)) >= 0.1:
        parts.append(f"أداء السهم مقابل المؤشر: {safe_round(rel_market, 1)}%")
    if sector_symbol and abs(float(rel_sector or 0)) >= 0.1:
        parts.append(f"أداء السهم مقابل القطاع: {safe_round(rel_sector, 1)}%")
    return " - ".join(parts)


def get_market_sector_context(symbol: str, sector: str, industry: str = "", daily_bars=None) -> dict:
    try:
        symbol = str(symbol or "").upper().strip()
        sector, industry = _normalize_context_inputs(symbol, sector, industry)
        stock_bars = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        stock_return_20 = _context_return_pct(stock_bars, 20)
        benchmark_symbol = _context_benchmark_for_sector(sector, industry)
        sector_symbol = _context_sector_etf(sector, industry)

        bench_key = f"ctx::{benchmark_symbol}"
        bench_cached = CONTEXT_CACHE.get(bench_key)
        if bench_cached is None:
            bench_bars = get_daily_bars(benchmark_symbol)
            bench_cached = {
                "trend": get_trend(benchmark_symbol, bench_bars).get("trend", "unknown"),
                "return_20": _context_return_pct(bench_bars, 20),
            }
            CONTEXT_CACHE[bench_key] = bench_cached

        sector_cached = None
        if sector_symbol:
            sec_key = f"ctx::{sector_symbol}"
            sector_cached = CONTEXT_CACHE.get(sec_key)
            if sector_cached is None:
                sec_bars = get_daily_bars(sector_symbol)
                sector_cached = {
                    "trend": get_trend(sector_symbol, sec_bars).get("trend", "unknown"),
                    "return_20": _context_return_pct(sec_bars, 20),
                }
                CONTEXT_CACHE[sec_key] = sector_cached

        benchmark_trend = str((bench_cached or {}).get("trend", "unknown") or "unknown")
        benchmark_return_20 = float((bench_cached or {}).get("return_20", 0) or 0)
        rel_vs_market = float(stock_return_20 or 0) - benchmark_return_20
        market_support_score = _context_trend_points(benchmark_trend) + _context_relative_points(rel_vs_market)
        market_support_label = _context_support_label(market_support_score)

        sector_trend = str((sector_cached or {}).get("trend", "unknown") or "unknown") if sector_cached else "unknown"
        sector_return_20 = float((sector_cached or {}).get("return_20", 0) or 0) if sector_cached else 0.0
        rel_vs_sector = float(stock_return_20 or 0) - sector_return_20 if sector_cached else 0.0
        sector_support_score = (_context_trend_points(sector_trend) + _context_relative_points(rel_vs_sector)) if sector_cached else 0
        sector_support_label = _context_support_label(sector_support_score) if sector_cached else ("محايد" if sector_symbol else "غير متوفر")

        total_score = max(-18, min(18, int(round((market_support_score * 0.8) + (sector_support_score * 1.2)))))
        if not sector_symbol:
            total_score = max(-18, min(18, total_score - 3))
        alignment_label = _context_alignment_label(total_score)
        alignment_detail = _context_alignment_detail(market_support_label, sector_support_label, benchmark_symbol, sector_symbol, rel_vs_market, rel_vs_sector)

        return {
            "benchmark_symbol": benchmark_symbol,
            "benchmark_trend": benchmark_trend,
            "benchmark_return_20_pct": safe_round(benchmark_return_20, 2),
            "stock_return_20_pct": safe_round(stock_return_20, 2),
            "relative_to_market_pct": safe_round(rel_vs_market, 2),
            "market_support_score": market_support_score,
            "market_support_label": market_support_label,
            "sector_etf_symbol": sector_symbol,
            "sector_trend": sector_trend,
            "sector_return_20_pct": safe_round(sector_return_20, 2),
            "relative_to_sector_pct": safe_round(rel_vs_sector, 2),
            "sector_support_score": sector_support_score,
            "sector_support_label": sector_support_label,
            "market_sector_score": total_score,
            "market_sector_alignment_label": alignment_label,
            "market_sector_alignment_detail": alignment_detail,
        }
    except Exception as exc:
        try:
            symbol = str(symbol or "").upper().strip()
            sector, industry = _normalize_context_inputs(symbol, sector, industry)
            fallback_benchmark = _context_benchmark_for_sector(sector, industry)
            fallback_sector = _context_sector_etf(sector, industry)
        except Exception:
            fallback_benchmark = "SPY"
            fallback_sector = ""
        try:
            print(f"MARKET_SECTOR_CONTEXT_ERROR: {symbol} | {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return {
            "benchmark_symbol": fallback_benchmark or "SPY",
            "benchmark_trend": "unknown",
            "benchmark_return_20_pct": 0.0,
            "stock_return_20_pct": 0.0,
            "relative_to_market_pct": 0.0,
            "market_support_score": 0,
            "market_support_label": "غير واضح",
            "sector_etf_symbol": fallback_sector or "",
            "sector_trend": "unknown",
            "sector_return_20_pct": 0.0,
            "relative_to_sector_pct": 0.0,
            "sector_support_score": 0,
            "sector_support_label": "محايد" if fallback_sector else "غير متوفر",
            "market_sector_score": 0,
            "market_sector_alignment_label": "محايد",
            "market_sector_alignment_detail": _context_alignment_detail("غير واضح", "محايد" if fallback_sector else "غير متوفر", fallback_benchmark or "SPY", fallback_sector or "", 0, 0),
            "historical_behavior_score": 50.0,
            "historical_context_score": 50.0,
            "historical_context_label": "محايد",
            "historical_context_detail": "لا توجد بيانات كافية لربط السهم بالمؤشر والقطاع.",
        }

