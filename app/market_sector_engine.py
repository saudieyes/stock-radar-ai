from .settings import CONTEXT_CACHE, SECTOR_ETF_MAP
from .utils import *
from .data_loader import COMPANIES_DATA, SECTOR_DATA
from .market_data import get_daily_bars, get_trend

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

    # direct map from settings first
    for key, value in SECTOR_ETF_MAP.items():
        if str(key).lower() in combined:
            return value

    if any(x in combined for x in [
        "technology", "information technology", "semiconductor", "chip", "software", "cloud", "cyber",
        "computer", "electronic computer", "consumer electronics", "hardware", "data processing",
        "communications equipment"
    ]):
        return "XLK"
    if any(x in combined for x in ["communication services", "internet", "streaming", "social media", "telecom", "wireless", "search"]):
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
        sector_support_label = _context_support_label(sector_support_score) if sector_cached else "غير متوفر"

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
    except:
        return {
            "benchmark_symbol": "SPY",
            "benchmark_trend": "unknown",
            "benchmark_return_20_pct": 0.0,
            "stock_return_20_pct": 0.0,
            "relative_to_market_pct": 0.0,
            "market_support_score": 0,
            "market_support_label": "غير واضح",
            "sector_etf_symbol": "",
            "sector_trend": "unknown",
            "sector_return_20_pct": 0.0,
            "relative_to_sector_pct": 0.0,
            "sector_support_score": 0,
            "sector_support_label": "غير متوفر",
            "market_sector_score": 0,
            "market_sector_alignment_label": "محايد",
            "market_sector_alignment_detail": "لا توجد بيانات كافية عن المؤشر والقطاع.",
            "historical_behavior_score": 50.0,
            "historical_context_score": 50.0,
            "historical_context_label": "محايد",
            "historical_context_detail": "لا توجد بيانات كافية لربط السهم بالمؤشر والقطاع.",
        }
