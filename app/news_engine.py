from collections import Counter

from .settings import NEWS_SCOPE_LABELS, POLYGON_API_KEY
from .utils import *
from .utils import _cache_get, _cache_set
from .market_data import http_get_json
from .settings import AI_NEWS_ENABLED, AI_NEWS_MAX_CLASSIFY_PER_SYMBOL, AI_NEWS_MIN_CONFIDENCE
from .ai_news_classifier import (
    classify_news_with_ai,
    format_news_time_labels,
    get_ai_news_status,
)

NEWS_CACHE = {}
NEWS_CACHE_TTL_SECONDS = 900
NEWS_DIAGNOSTICS = {}

def _record_news_diag(symbol: str, diag: dict) -> None:
    try:
        NEWS_DIAGNOSTICS[normalize_symbol_text(symbol)] = dict(diag or {})
        if len(NEWS_DIAGNOSTICS) > 500:
            for k in list(NEWS_DIAGNOSTICS.keys())[:100]:
                NEWS_DIAGNOSTICS.pop(k, None)
    except Exception:
        pass

def get_news_diagnostics(symbol: str | None = None) -> dict:
    try:
        if symbol:
            key = normalize_symbol_text(symbol)
            return dict(NEWS_DIAGNOSTICS.get(key, {}) or {})
        return {k: dict(v or {}) for k, v in list(NEWS_DIAGNOSTICS.items())[-80:]}
    except Exception:
        return {}

def _diag_reject(counter: Counter, reason: str) -> None:
    try:
        counter[str(reason or "unknown")] += 1
    except Exception:
        pass


def empty_news_bundle() -> dict:
    return {
        "news_note": "لا يوجد خبر أو محفز حديث",
        "news_title": "",
        "news_badge": "",
        "news_category": "neutral",
        "news_sentiment": "neutral",
        "news_scope": "neutral",
        "news_scope_label": "خبر محايد",
        "news_freshness_label": "",
        "news_published_utc": "",
        "news_sessions_since": 999,
        "news_effect_score": 0,
        "news_is_catalyst": False,
        "news_context_note": "لا يوجد خبر أو محفز حديث يمكن الاعتماد عليه الآن.",
        "news_related_tickers_count": 0,
        "news_published_ksa": "",
        "news_age_label": "",
        "news_source_name": "",
        "news_ai": {},
        "news_public_summary": "",
        "news_context_only": False,
        "catalyst_score": 0,
    }

def is_company_warning_news(text_lower: str) -> bool:
    txt = f" {normalize_text(text_lower)} "
    warning_phrases = [
        " founder sold ", " co founder sold ", " co-founder sold ", " insider sold ",
        " insiders sold ", " insider selling ", " insider sale ", " insider sells ",
        " ceo sold ", " cfo sold ", " director sold ", " executive sold ",
        " sold shares worth ", " sold stock worth ", " sells shares worth ",
        " should investors avoid ", " should you avoid ", " avoid the stock ",
        " avoid this stock ", " avoid shares ", " stock to avoid ",
        " shares tumble ", " shares plunge ", " shares sink ", " shares fall ",
        " cuts price target ", " price target cut ", " lowers price target ",
        " downgrades ", " downgraded ", " downgrade ",
        " weak guidance ", " cuts guidance ", " missed estimates ", " misses estimates ",
        " public offering ", " secondary offering ", " registered direct offering ",
        " dilution ", " going concern ", " delisting ", " bankruptcy ",
    ]
    return any(p in txt for p in warning_phrases)


def trading_sessions_since_news(published_utc: str) -> int:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        current_trade_date = now_ny.date()
        if current_trade_date.weekday() >= 5:
            current_trade_date = prev_business_day(current_trade_date)
        elif (now_ny.hour * 60 + now_ny.minute) < (9 * 60 + 30):
            current_trade_date = prev_business_day(current_trade_date - timedelta(days=1))

        published = datetime.fromisoformat(str(published_utc).replace("Z", "+00:00"))
        pub_ny = published.astimezone(ny)
        reaction_date = pub_ny.date()
        minutes = pub_ny.hour * 60 + pub_ny.minute

        if reaction_date.weekday() >= 5:
            reaction_date = next_business_day(reaction_date)
        elif minutes >= 16 * 60:
            reaction_date = next_business_day(reaction_date + timedelta(days=1))
        else:
            reaction_date = next_business_day(reaction_date)

        return count_business_days_exclusive(reaction_date, current_trade_date)
    except:
        return 999


def classify_news_impact(title_lower: str, sessions_since: int):
    return classify_news_effect("company", detect_news_sentiment(title_lower), sessions_since), ""


def get_news_session_limit(scope: str, sentiment: str) -> int:
    scope = str(scope or "neutral")
    sentiment = str(sentiment or "neutral")
    if scope in {"market", "opinion", "neutral", "unrelated"}:
        return 0
    if sentiment == "positive":
        return POSITIVE_NEWS_MAX_SESSIONS
    if sentiment in {"negative", "legal"}:
        return NEGATIVE_NEWS_MAX_SESSIONS
    return 0


def is_news_within_session_limit(scope: str, sentiment: str, sessions_since: int) -> bool:
    limit = get_news_session_limit(scope, sentiment)
    if limit <= 0:
        return False
    try:
        sessions = 999 if sessions_since is None else int(sessions_since)
    except Exception:
        sessions = 999
    return sessions <= limit


def classify_news_freshness_label(sessions_since: int) -> tuple[str, int]:
    # Backward-compatible trading-session freshness. UI labels prefer clock age.
    if sessions_since <= 0:
        return "حديث جدًا", 100
    if sessions_since == 1:
        return "حديث", 78
    if sessions_since == 2:
        return "حديث نسبيًا", 52
    if sessions_since == 3:
        return "أقدم قليلًا", 28
    if sessions_since <= 5:
        return "قديم", 12
    return "قديم جدًا", 4


def news_age_hours(published_utc: str) -> float | None:
    try:
        if not published_utc:
            return None
        dt = datetime.fromisoformat(str(published_utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        return None


def classify_news_freshness_from_published(published_utc: str, sessions_since: int = 999) -> tuple[str, int]:
    """Clock-based user-facing recency.

    A story from 3-5 hours ago is still حديث جدًا. Whether it gets Catalyst
    points is a separate materiality/scope decision.
    """
    hours = news_age_hours(published_utc)
    if hours is None:
        return classify_news_freshness_label(sessions_since)
    if hours <= 12:
        return "حديث جدًا", 100
    if hours <= 36:
        return "حديث", 82
    if hours <= 72:
        return "حديث نسبيًا", 58
    if hours <= 120:
        return "أقدم قليلًا", 34
    if hours <= 240:
        return "قديم", 12
    return "قديم جدًا", 4


def news_scope_label(scope: str) -> str:
    return NEWS_SCOPE_LABELS.get(str(scope or "neutral"), "محايد")


def get_sector_name_variants(sector: str, industry: str) -> list[str]:
    stop_words = {
        "and", "other", "general", "specialty", "services", "service", "products", "product",
        "industries", "industry", "consumer", "capital", "markets", "market", "systems", "system",
        "equipment", "devices", "technology", "technologies", "communications", "communication"
    }
    variants = set()
    for raw in [sector, industry]:
        txt = normalize_text(raw)
        if not txt:
            continue
        variants.add(txt)
        parts = [p for p in txt.split() if p and p not in stop_words]
        if len(parts) >= 2:
            variants.add(" ".join(parts[:2]))
        for part in parts:
            if len(part) >= 4:
                variants.add(part)

        if "semiconductor" in txt or "chip" in txt:
            variants.update({"semiconductor", "semiconductors", "chip", "chips"})
        if "software" in txt or "cloud" in txt:
            variants.update({"software", "cloud", "saas"})
        if "biotech" in txt or "pharma" in txt or "drug" in txt:
            variants.update({"biotech", "biotechnology", "pharma", "pharmaceutical", "drug", "drugs"})
        if "oil" in txt or "energy" in txt or "gas" in txt:
            variants.update({"oil", "crude", "energy", "gas"})
        if "bank" in txt or "financial" in txt:
            variants.update({"bank", "banks", "financial", "financials"})
        if "insurance" in txt:
            variants.update({"insurance", "insurer", "insurers"})

    cleaned = []
    for v in variants:
        v = v.strip()
        if len(v) >= 4:
            cleaned.append(v)
    return list(dict.fromkeys(cleaned))


def detect_news_shape(text_lower: str, related_count: int = 0) -> str:
    # Warning/company-risk headlines must not be treated as positive opinion pieces.
    # Example: "Founder sold shares... Should investors avoid?" is a cautionary company event.
    if is_company_warning_news(text_lower):
        return "direct"
    opinion_markers = [
        "opinion", "analysis", "article", "my take", "why i like", "why i love",
        "earnings transcript", "conference call transcript", "transcript", "prepared remarks",
        "is it finally time to buy", "is it time to buy", "should you buy", "buy now", "sell now",
        "according to wall street", "wall street thinks", "analyst says", "analysts say",
        "cheap stock", "cheap cloud stock", "best stock", "best stocks", "top stock", "top stocks",
        "stock to buy", "stocks to buy", "stock to watch", "stocks to watch", "watch these stocks",
        "looks cheap", "or does it", "ready to break out", "break out in 2026", "breakout in 2026",
        "could make you", "millionaire", "millionaires", "top picks", "editorial", "motley fool", "seeking alpha",
        "here s why", "here's why", "here s what this means", "here's what this means",
        "prediction", "predictions", "predicts", "pick and shovel", "pick-and-shovel",
        "could follow suit", "could jump", "another incredibly", "best pick", "best ai stock",
        "wall street expects", "wall street forecast", "wall street projects", "top idea", "if you buy", "here s where it could be", "here's where it could be", "in 5 years", "in five years", "could be in 5 years", "best growth stock", "best value stock", "could soar", "rumors debunked", "stock could", "shares could"
    ]
    roundup_markers = [
        "top gainers", "top losers", "biggest gainers", "biggest losers", "market movers",
        "top movers", "trending stocks", "stocks in focus", "stocks to watch", "winners and losers",
        "stocks making moves", "stocks moving", "top performers", "led gains", "led losses",
        "rallied among", "among top", "weekly recap", "last week", "top 10", "top 5",
        "are among the top", "in your portfolio", "best performers", "top large cap gainers"
    ]

    if any(k in text_lower for k in opinion_markers):
        return "opinion"
    if any(k in text_lower for k in roundup_markers):
        return "roundup"
    if text_lower.startswith("why ") and " stock " in text_lower:
        return "opinion"
    if text_lower.startswith("is ") and " stock " in text_lower:
        return "opinion"
    if text_lower.endswith("?") and (" stock " in text_lower or " shares " in text_lower):
        return "opinion"
    if text_lower.startswith("prediction") or text_lower.startswith("opinion"):
        return "opinion"
    if " why " in f" {text_lower} " and (" stock is " in text_lower or " shares are " in text_lower):
        return "opinion"
    if " looks cheap " in f" {text_lower} " or " ready to break out " in f" {text_lower} ":
        return "opinion"
    if related_count >= 2 and any(k in text_lower for k in ["according to wall street", "prediction", "top", "best", "cheap stock"]):
        return "opinion"
    if related_count >= 3 and any(k in text_lower for k in ["top", "gainers", "losers", "rally", "rallies", "soared", "soaring"]):
        return "roundup"
    return "direct"


def has_direct_company_event_signal(text_lower: str) -> bool:
    event_markers = [
        "earnings", "results", "guidance", "ceo", "cfo", "step down", "steps down", "resign", "resigns",
        "appoints", "appoint", "launches", "launch", "approval", "approved", "wins", "win", "contract",
        "partnership", "acquires", "acquisition", "merger", "merges", "investigation", "investigates",
        "lawsuit", "class action", "investor alert", "subpoena", "recall", "offering", "buyback",
        "announces", "announced", "reports", "reported", "secures", "expands", "expansion",
        "opens", "opening of", "opening", "agreement", "deal", "acquire", "acquires", "acquiring", "to acquire", "participate in", "participates in", "conference", "summit", "presents at", "presentation",
        "insider sold", "insider selling", "founder sold", "ceo sold", "cfo sold", "director sold", "sold shares",
        "should investors avoid", "avoid the stock", "downgrade", "downgrades", "price target cut", "cuts price target"
    ]
    return any(k in text_lower for k in event_markers)


def detect_news_sentiment(text_lower: str, related_count: int = 0) -> str:
    if is_company_warning_news(text_lower):
        return "negative"
    shape = detect_news_shape(text_lower, related_count)
    if shape in {"opinion", "roundup"}:
        return "opinion"

    legal_negative = [
        "lawsuit", "investigation", "investigates", "class action", "investor alert", "law firm",
        "lead plaintiff", "securities class action", "claims on behalf", "probe", "subpoena"
    ]
    negative_keywords = [
        "miss", "misses", "cuts guidance", "guidance cut", "downgrade", "dilution", "warning",
        "declines", "falls", "plunges", "recall", "delay", "bankruptcy", "default", "selloff",
        "pulls back", "pullback", "tension", "tensions", "tariff", "tariffs", "conflict", "war",
        "pressure", "risk off", "slump", "slumps", "crash", "crashed", "fraud", "short report",
        "weak earnings", "missed estimates", "insider sold", "shareholder alert", "steps down", "step down",
        "founder sold", "co founder sold", "co-founder sold", "insider selling", "insider sale",
        "ceo sold", "cfo sold", "director sold", "executive sold", "sold shares worth",
        "should investors avoid", "should you avoid", "avoid the stock", "stock to avoid",
        "cuts price target", "price target cut", "lowers price target", "downgraded"
    ]
    offering_negative = [
        "public offering", "pricing of public offering", "registered direct offering", "secondary offering",
        "underwritten offering", "pricing of common stock", "prices public offering", "follow-on offering"
    ]
    positive_keywords = [
        "beat", "beats", "strong guidance", "raises guidance", "buyback", "surge", "jumps", "soars",
        "wins", "upgrade", "partnership", "contract", "record revenue", "secures", "launch",
        "breakthrough", "approval", "expands", "growth", "record", "tops estimates", "award",
        "awarded", "agreement", "deal", "signs", "signed", "selected", "collaboration", "teams up",
        "rebound", "rally", "gains", "strong demand", "new order", "orders", "opening of", "opens"
    ]

    if any(k in text_lower for k in legal_negative):
        return "legal"
    if any(k in text_lower for k in offering_negative):
        return "negative"

    positive_hits = sum(1 for k in positive_keywords if k in text_lower)
    negative_hits = sum(1 for k in negative_keywords if k in text_lower)

    if negative_hits and not positive_hits:
        return "negative"
    if positive_hits and not negative_hits:
        return "positive"
    if negative_hits and positive_hits:
        return "mixed"
    return "neutral"


def detect_news_category(title_lower: str) -> str:
    sentiment = detect_news_sentiment(title_lower)
    return "neutral" if sentiment == "opinion" else sentiment


def detect_news_scope(symbol: str, company_variants: list[str], sector_variants: list[str], related: list[str], text_lower: str, sentiment: str) -> str:
    market_keywords = [
        "stock market today", "market today", "s p 500", "sp 500", "dow jones", "nasdaq", "wall street",
        "federal reserve", "fed", "fomc", "interest rate", "rate cut", "rate hike", "treasury yield",
        "treasury yields", "inflation", "cpi", "ppi", "jobs report", "oil", "crude", "hormuz",
        "macro", "risk off", "risk on", "broad market", "market rally", "market selloff"
    ]

    related_count = len(list(related or []))
    company_hit = any(v and v in text_lower for v in company_variants)
    sector_hit = any(v and v in text_lower for v in sector_variants)
    symbol_hit = symbol.lower() in text_lower or symbol in related
    market_hit = any(k in text_lower for k in market_keywords)
    shape = detect_news_shape(text_lower, related_count)
    direct_event = has_direct_company_event_signal(text_lower)

    if sentiment == "opinion" or shape in {"opinion", "roundup"}:
        return "opinion"

    # Direct symbol/company mention with a real event is company news first, even if many tickers are attached.
    if (symbol_hit or company_hit) and direct_event and not market_hit:
        return "company"

    if related_count >= 2 and not direct_event:
        if sector_hit and sector_variants:
            return "sector"
        if market_hit:
            return "market"
        return "unrelated"

    if market_hit and (not company_hit or not direct_event):
        if not company_hit and not sector_hit:
            return "market"
        if company_hit and not direct_event:
            return "market"
    if company_hit and (direct_event or related_count <= 1 or symbol_hit):
        return "company"
    if symbol_hit and not market_hit and (related_count <= 1 or direct_event):
        return "company"
    if sector_hit and sector_variants:
        return "sector"
    if market_hit:
        return "market"
    return "neutral"


def classify_news_effect(scope: str, sentiment: str, sessions_since: int) -> int:
    base_map = {
        ("company", "positive"): 6,
        ("company", "negative"): -6,
        ("company", "legal"): -7,
        ("sector", "positive"): 3,
        ("sector", "negative"): -3,
        ("sector", "legal"): -4,
        ("market", "positive"): 1,
        ("market", "negative"): -1,
    }
    base = int(base_map.get((str(scope or "neutral"), str(sentiment or "neutral")), 0) or 0)
    if base == 0:
        return 0

    is_positive = base > 0
    if is_positive:
        if sessions_since <= 0:
            factor = 1.0
        elif sessions_since == 1:
            factor = 0.70
        elif sessions_since == 2:
            factor = 0.35
        elif sessions_since == 3:
            factor = 0.15
        else:
            factor = 0.0
    else:
        if sessions_since <= 0:
            factor = 1.0
        elif sessions_since == 1:
            factor = 0.85
        elif sessions_since == 2:
            factor = 0.60
        elif sessions_since <= 5:
            factor = 0.35
        elif sentiment == "legal" and scope == "company" and sessions_since <= 10:
            factor = 0.20
        else:
            factor = 0.0

    effect = int(round(base * factor))
    if effect == 0:
        if not is_positive and sessions_since <= 2:
            return -1
        if is_positive and sessions_since <= 1:
            return 1
    return effect


def build_news_badge(scope: str, sentiment: str, related_count: int = 0) -> str:
    scope = str(scope or "neutral")
    sentiment = str(sentiment or "neutral")
    if scope == "company":
        if sentiment == "legal":
            return "⛔ خبر قانوني مباشر"
        if sentiment == "negative":
            return "🔴 خبر شركة سلبي"
        if sentiment == "positive":
            return "🟢 خبر شركة إيجابي"
        if sentiment == "mixed":
            return "🟡 خبر شركة مختلط"
        return "⚪ خبر شركة محايد"
    if scope == "sector":
        if sentiment == "positive":
            return "🏭 سياق قطاعي داعم"
        if sentiment in {"negative", "legal"}:
            return "🏭 سياق قطاعي ضاغط"
        if sentiment == "mixed":
            return "🏭 سياق قطاعي مختلط"
        return "🏭 سياق قطاعي فقط"
    if scope == "market":
        return "📰 سياق سوق عام"
    if scope == "opinion":
        return "🚫 مقال رأي غير محسوب"
    if scope in {"unrelated", "neutral"}:
        return ""
    return ""


def build_news_context_note(scope: str, sentiment: str, freshness_label: str, effect_score: int, related_count: int = 0) -> str:
    freshness = f" ({freshness_label})" if freshness_label else ""
    stale = freshness_label in {"قديم", "قديم جدًا"}
    oldish = freshness_label in {"أقدم قليلًا"}

    if scope == "company":
        if sentiment == "positive":
            if stale:
                return f"خبر شركة إيجابي لكنه قديم نسبيًا؛ يُعرض للمعلومة ولا يُستخدم كمحفز ساخن{freshness}."
            if oldish:
                return f"خبر شركة إيجابي لكنه أقدم قليلًا؛ يمكن اعتباره دعمًا خفيفًا لا محفزًا قويًا{freshness}."
            if effect_score <= 0:
                return f"خبر شركة إيجابي/داعم، لكن أثره محدود أو محايد؛ يُعرض للمعلومة ولا يضيف نقاط Catalyst{freshness}."
            return f"خبر شركة مباشر وحديث ومؤثر؛ يمكن أن يدعم الفكرة{freshness}."
        if sentiment == "legal":
            return f"خبر قانوني مباشر على الشركة ويُعامل كعامل ضغط واضح{freshness}."
        if sentiment == "negative":
            return f"خبر سلبي مباشر على الشركة ويُخصم من الفكرة الأساسية{freshness}."
        if sentiment == "mixed":
            return f"خبر مباشر يخص الشركة لكنه مختلط؛ يُعرض للمعلومة ولا يُحسب كمحفز إيجابي{freshness}."
        return f"خبر يخص الشركة لكنه محايد أو محدود الأثر؛ لا يضيف نقاط Catalyst{freshness}."

    if scope == "sector":
        if sentiment == "positive":
            return f"سياق قطاعي داعم فقط؛ يفيد الفهم العام ولا يضيف نقاط Catalyst ولا يُعامل كخبر شركة مباشر{freshness}."
        if sentiment in {"negative", "legal"}:
            return f"سياق قطاعي ضاغط فقط؛ قد يشرح البيئة العامة لكنه لا يُعامل كخبر شركة مباشر{freshness}."
        if sentiment == "mixed":
            return f"سياق قطاعي مختلط فقط؛ لا يضيف نقاط Catalyst ولا يغيّر جودة السهم وحده{freshness}."
        return f"سياق قطاعي فقط؛ يفيد الفهم العام لكنه ليس محفزًا مباشرًا للسهم{freshness}."

    if scope == "market":
        return f"سياق سوق عام فقط؛ لا يخص الشركة مباشرة ولا يضيف نقاط Catalyst{freshness}."
    if scope == "opinion":
        return f"مقال رأي أو قائمة ترشيحات؛ لا يُعامل كمحفز ولا يضيف نقاط Catalyst{freshness}."
    if scope == "unrelated":
        return "خبر غير ذي صلة مباشرة بالسهم؛ لا يُعرض كمحفز ولا يضيف نقاطًا."
    return f"لا يوجد خبر شركة مباشر مؤثر يمكن الاعتماد عليه الآن{freshness}."


def build_public_news_summary(scope: str, sentiment: str, title: str = "") -> str:
    """Short user-facing note. Keep long AI reasons in debug/news_ai only."""
    scope = str(scope or "neutral")
    sentiment = str(sentiment or "neutral")
    if scope == "company":
        if sentiment == "positive":
            return "خبر شركة مباشر داعم"
        if sentiment == "negative":
            return "خبر شركة مباشر سلبي"
        if sentiment == "legal":
            return "خبر قانوني مباشر على الشركة"
        if sentiment == "mixed":
            return "خبر شركة مختلط — لا يضيف نقاطًا إيجابية"
        return "خبر شركة محايد — لا يضيف نقاط Catalyst"
    if scope == "sector":
        return "سياق قطاعي فقط — لا يضيف نقاط Catalyst"
    if scope == "market":
        return "سياق سوق عام فقط — لا يخص الشركة مباشرة"
    if scope == "opinion":
        return "رأي أو مقال تحليلي — لا يُحسب كمحفز"
    if scope == "unrelated":
        return "خبر غير ذي صلة مباشرة — لا يُحسب كمحفز"
    return "لا يوجد خبر شركة مباشر معتمد"


def force_neutral_low_materiality_company_news(title_text: str, sentiment: str, materiality: str, catalyst_allowed: bool) -> tuple[str, str, bool, str]:
    """Prevent routine/low-impact company items from being displayed as positive catalysts."""
    txt = normalize_text(title_text)
    routine_dividend = (
        "dividend" in txt
        and not any(k in txt for k in ["raises dividend", "raise dividend", "increases dividend", "increased dividend", "special dividend", "dividend hike"])
    )
    routine_events = [
        "declares quarterly dividend", "announces quarterly dividend", "sets quarterly dividend",
        "participate in", "participates in", "to participate", "presents at", "present at",
        "conference", "investor conference", "webcast", "fireside chat",
    ]
    if routine_dividend or any(k in txt for k in routine_events):
        return "neutral", "low", False, "خبر روتيني/محدود الأثر؛ لا يعامل كمحفز."
    if str(sentiment or "") == "positive" and str(materiality or "") == "low" and not catalyst_allowed:
        return "neutral", "low", False, "خبر إيجابي منخفض الأهمية؛ يعرض للمعلومة فقط."
    return sentiment, materiality, catalyst_allowed, ""


def apply_ai_news_classification(candidate: dict, ai: dict | None, sector: str = "", industry: str = "") -> dict:
    """Apply AI classification defensively. AI can remove/restrict catalyst points, not force risky scoring."""
    try:
        if not isinstance(ai, dict) or not ai.get("ok"):
            if isinstance(ai, dict) and ai.get("error"):
                candidate["ai_news"] = {"ok": False, "error": ai.get("error")}
            return candidate
        out = dict(candidate)
        scope = str(ai.get("scope", "neutral") or "neutral")
        sentiment = str(ai.get("sentiment", "neutral") or "neutral")
        materiality = str(ai.get("materiality", "low") or "low")
        confidence = int(ai.get("confidence", 0) or 0)
        is_opinion = bool(ai.get("is_opinion"))
        direct_company = bool(ai.get("is_direct_company_news"))
        catalyst_allowed = bool(ai.get("catalyst_allowed"))
        reason = str(ai.get("reason", "") or "").strip()

        combined_title = f"{out.get('title', '')} {out.get('description', '')}"
        sentiment, materiality, catalyst_allowed, neutral_reason = force_neutral_low_materiality_company_news(
            combined_title, sentiment, materiality, catalyst_allowed
        )
        if neutral_reason:
            reason = (neutral_reason + (" " + reason if reason else "")).strip()

        # Logical guard: if sector/industry is unknown, the tool must not claim a sector catalyst.
        if scope == "sector" and not (str(sector or "").strip() or str(industry or "").strip()):
            scope = "neutral"
            catalyst_allowed = False
            reason = (reason + " | لا توجد بيانات قطاع/صناعة كافية، لذلك لا نعامله كخبر قطاعي.").strip(" |")

        if is_opinion or scope == "opinion":
            scope = "opinion"
            sentiment = "opinion"
            materiality = "low"
            catalyst_allowed = False
            direct_company = False
        elif scope == "company" and not direct_company:
            catalyst_allowed = False
        elif scope == "unrelated":
            catalyst_allowed = False
        elif scope != "company":
            # Sector/market/opinion context is useful, but it must not lift a stock as a catalyst.
            catalyst_allowed = False
        elif confidence < int(AI_NEWS_MIN_CONFIDENCE or 70):
            catalyst_allowed = False
        elif materiality == "low":
            catalyst_allowed = False

        if sentiment == "mixed":
            impact = 0
        elif catalyst_allowed and scope == "company" and sentiment in {"positive", "negative", "legal"}:
            raw_sessions = out.get("sessions_since", 999)
            try:
                sessions_for_effect = 999 if raw_sessions is None else int(raw_sessions)
            except Exception:
                sessions_for_effect = 999
            impact = classify_news_effect(scope, sentiment, sessions_for_effect)
        else:
            impact = 0

        out["scope"] = scope
        out["sentiment"] = sentiment
        out["category"] = "opinion" if scope == "opinion" else sentiment if sentiment in {"positive", "negative", "legal", "mixed"} else "neutral"
        out["impact"] = impact
        out["scope_label"] = news_scope_label(scope)
        out["badge"] = build_news_badge(scope, sentiment, int(out.get("related_count", 0) or 0))
        out["context_note"] = build_news_context_note(scope, sentiment, out.get("freshness_label", ""), impact, int(out.get("related_count", 0) or 0))
        public_note = build_public_news_summary(scope, sentiment, out.get("title", ""))
        out["public_news_summary"] = public_note
        out["ai_news"] = {
            "ok": True,
            "scope": scope,
            "sentiment": sentiment,
            "materiality": materiality,
            "confidence": confidence,
            "is_opinion": is_opinion,
            "is_direct_company_news": direct_company,
            "catalyst_allowed": catalyst_allowed,
            "reason": reason,
            "public_note": public_note,
            "cache_hit": bool(ai.get("cache_hit")),
        }
        return out
    except Exception as exc:
        candidate["ai_news"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return candidate


def get_news_bundle(symbol, company_name="", sector="", industry=""):
    symbol = normalize_symbol_text(symbol)
    bundle = empty_news_bundle()
    diag = {
        "symbol": symbol,
        "provider": "polygon",
        "status": "init",
        "raw_count": 0,
        "candidate_count": 0,
        "actionable_count": 0,
        "context_count": 0,
        "rejected_count": 0,
        "rejected_reasons": {},
        "sample_raw_titles": [],
        "sample_candidates": [],
        "selected_title": "",
        "selected_scope": "",
        "selected_sentiment": "",
        "selected_effect": 0,
        "fallback_context_used": False,
        "ai_enabled": bool(AI_NEWS_ENABLED),
        "ai_used": False,
        "ai_cache_hit": False,
        "ai_error": "",
        "ai_result": {},
        "error": "",
    }
    try:
        cache_key = f"{symbol}|{str(company_name or '')[:60]}|{str(sector or '')[:40]}|{str(industry or '')[:40]}"
        cached = _cache_get(NEWS_CACHE, cache_key)
        if cached is not None:
            cached_bundle = dict(cached)
            cached_diag = dict(cached_bundle.get("news_debug", {}) or {})
            if cached_diag:
                cached_diag["cache_hit"] = True
                _record_news_diag(symbol, cached_diag)
            return cached_bundle

        if not POLYGON_API_KEY:
            diag["status"] = "missing_polygon_api_key"
            bundle["news_debug"] = diag
            _record_news_diag(symbol, diag)
            return _cache_set(NEWS_CACHE, cache_key, bundle, NEWS_CACHE_TTL_SECONDS)

        url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=20&order=desc&sort=published_utc&apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        results = r.get("results", []) if isinstance(r, dict) else []
        diag["raw_count"] = len(results or [])
        diag["status"] = "no_raw_news" if not results else "raw_news_received"
        if not results:
            bundle["news_debug"] = diag
            _record_news_diag(symbol, diag)
            return _cache_set(NEWS_CACHE, cache_key, bundle, NEWS_CACHE_TTL_SECONDS)

        company_variants = get_company_name_variants(company_name)
        sector_variants = get_sector_name_variants(sector, industry)
        best_actionable = None
        best_actionable_score = -9999
        best_context = None
        best_context_score = -9999
        reject_counts = Counter()

        for item in results:
            title = str(item.get("title", "") or "").strip()
            desc = str(item.get("description", "") or item.get("summary", "") or "").strip()
            if title and len(diag["sample_raw_titles"]) < 8:
                diag["sample_raw_titles"].append(title[:220])
            if not title:
                _diag_reject(reject_counts, "empty_title")
                continue

            full_text = normalize_text(f"{title} {desc}")
            related = [str(x).upper().strip() for x in item.get("tickers", []) if str(x).strip()]
            related_count = len(related)
            published_utc = str(item.get("published_utc", "") or "")
            published_ksa, age_label = format_news_time_labels(published_utc)
            sessions_since = trading_sessions_since_news(published_utc)
            freshness_label, freshness_score = classify_news_freshness_from_published(published_utc, sessions_since)
            publisher_obj = item.get("publisher", {}) if isinstance(item, dict) else {}
            publisher_name = str((publisher_obj or {}).get("name", "") or "") if isinstance(publisher_obj, dict) else ""
            sentiment = detect_news_sentiment(full_text, related_count)
            scope = detect_news_scope(symbol, company_variants, sector_variants, related, full_text, sentiment)
            effect = classify_news_effect(scope, sentiment, sessions_since)
            badge = build_news_badge(scope, sentiment, related_count)
            context_note = build_news_context_note(scope, sentiment, freshness_label, effect, related_count)
            direct_event = has_direct_company_event_signal(full_text)
            news_shape = detect_news_shape(full_text, related_count)

            relevance = 0
            if symbol in related:
                relevance += 4
            if any(v and v in full_text for v in company_variants):
                relevance += 3
            if any(v and v in full_text for v in sector_variants):
                relevance += 2
            if direct_event and scope == "company":
                relevance += 2
            if related_count >= 2 and scope == "company" and not direct_event:
                relevance -= 8
            if news_shape == "roundup":
                relevance -= 12
            elif news_shape == "opinion":
                relevance -= 14

            scope_priority = {"company": 64, "sector": 32, "market": 14, "neutral": 4, "opinion": -24, "unrelated": -26}.get(scope, 0)
            sentiment_bonus = {"positive": 3, "negative": 5, "legal": 10, "neutral": 0, "opinion": -10}.get(sentiment, 0)
            candidate_score = scope_priority + relevance + freshness_score + (abs(effect) * 5) + sentiment_bonus
            if scope == "market" and sentiment == "neutral":
                candidate_score -= 8
            if scope in {"company", "sector"} and sentiment == "positive" and sessions_since >= 3:
                candidate_score -= 12
            if scope == "sector" and news_shape != "direct":
                candidate_score -= 10
            if scope == "company" and related_count >= 2 and not direct_event:
                candidate_score -= 8
            if effect == 0 and sentiment == "positive":
                candidate_score -= 8

            candidate = {
                "title": title,
                "description": desc,
                "publisher": publisher_name,
                "source_url": str(item.get("article_url", "") or item.get("url", "") or ""),
                "related_tickers": related,
                "impact": effect,
                "badge": badge,
                "scope": scope,
                "scope_label": news_scope_label(scope),
                "category": "neutral" if sentiment == "opinion" else sentiment,
                "sentiment": sentiment,
                "freshness_label": freshness_label,
                "published_utc": published_utc,
                "published_ksa": published_ksa,
                "age_label": age_label,
                "sessions_since": sessions_since,
                "context_note": context_note,
                "related_count": related_count,
                "shape": news_shape,
                "score": candidate_score,
            }
            diag["candidate_count"] += 1
            if len(diag["sample_candidates"]) < 8:
                diag["sample_candidates"].append({
                    "title": title[:180],
                    "scope": scope,
                    "sentiment": sentiment,
                    "shape": news_shape,
                    "sessions_since": sessions_since,
                    "effect": effect,
                    "score": round(candidate_score, 2),
                    "related_count": related_count,
                })

            context_ok = (
                scope in {"company", "sector"}
                and news_shape == "direct"
                and sessions_since <= 3
                and candidate_score > 20
                and sentiment in {"neutral", "positive", "negative", "legal", "mixed"}
            )
            if context_ok:
                diag["context_count"] += 1
                context_score = candidate_score + (8 if scope == "company" else 0) - (0 if sentiment != "neutral" else 4)
                if context_score > best_context_score:
                    best_context_score = context_score
                    best_context = dict(candidate)

            hard_reasons = []
            if news_shape in {"opinion", "roundup"}:
                hard_reasons.append(news_shape)
            if "transcript" in full_text or "prepared remarks" in full_text:
                hard_reasons.append("transcript")
            if not is_news_within_session_limit(scope, sentiment, sessions_since):
                hard_reasons.append("outside_session_limit_or_non_actionable")
            if scope != "company":
                hard_reasons.append(f"scope_{scope}_context_only")
            if sentiment not in {"positive", "negative", "legal"}:
                hard_reasons.append(f"sentiment_{sentiment}")
            if scope == "sector" and news_shape != "direct":
                hard_reasons.append("sector_not_direct")
            if scope == "company" and related_count >= 2 and not direct_event:
                hard_reasons.append("multi_ticker_without_company_event")
            if candidate_score <= 0:
                hard_reasons.append("low_score")

            if hard_reasons:
                diag["rejected_count"] += 1
                for reason in hard_reasons[:3]:
                    _diag_reject(reject_counts, reason)
                continue

            diag["actionable_count"] += 1
            if candidate_score > best_actionable_score:
                best_actionable_score = candidate_score
                best_actionable = candidate

        chosen = best_actionable
        fallback_context_used = False
        if not chosen and best_context:
            chosen = best_context
            fallback_context_used = True

        diag["rejected_reasons"] = dict(reject_counts.most_common(12))
        if not chosen:
            diag["status"] = "no_selected_news_after_filter"
            bundle["news_debug"] = diag
            _record_news_diag(symbol, diag)
            return _cache_set(NEWS_CACHE, cache_key, bundle, NEWS_CACHE_TTL_SECONDS)

        ai_result = None
        if AI_NEWS_ENABLED and AI_NEWS_MAX_CLASSIFY_PER_SYMBOL > 0:
            ai_result = classify_news_with_ai(symbol, company_name, sector, industry, chosen)
            if isinstance(ai_result, dict):
                diag["ai_used"] = bool(ai_result.get("ok"))
                diag["ai_cache_hit"] = bool(ai_result.get("cache_hit"))
                if ai_result.get("error"):
                    diag["ai_error"] = str(ai_result.get("error"))[:220]
                if ai_result.get("ok"):
                    diag["ai_result"] = {k: ai_result.get(k) for k in ["scope", "sentiment", "materiality", "confidence", "is_opinion", "is_direct_company_news", "catalyst_allowed", "reason", "cache_hit"]}
            chosen = apply_ai_news_classification(chosen, ai_result, sector, industry)

        chosen_scope = str(chosen.get("scope", "neutral") or "neutral")
        chosen_sentiment = str(chosen.get("sentiment", "neutral") or "neutral")
        title_to_show = chosen["title"]
        note_to_show = title_to_show or chosen.get("context_note", "")
        badge_to_show = chosen.get("badge", "")
        context_only_news = chosen_scope in {"sector", "market", "opinion", "unrelated", "neutral"}
        if context_only_news:
            # Context/opinion/market items may be shown for review, but never as stock catalysts.
            # Unrelated items stay hidden from the main news title.
            if chosen_scope == "unrelated":
                title_to_show = ""
                badge_to_show = ""
                note_to_show = build_public_news_summary(chosen_scope, chosen_sentiment, chosen.get("title", ""))
            else:
                title_to_show = chosen.get("title", "") or ""
                badge_to_show = chosen.get("badge", "") or ""
                note_to_show = title_to_show or build_public_news_summary(chosen_scope, chosen_sentiment, chosen.get("title", ""))

        impact = int(chosen.get("impact", 0) or 0)
        is_catalyst = (not fallback_context_used) and chosen_scope == "company" and impact != 0
        catalyst_score = impact if is_catalyst else 0
        context_note = chosen.get("context_note", "")
        time_bits = []
        if chosen.get("age_label"):
            time_bits.append(str(chosen.get("age_label")))
        if chosen.get("published_ksa"):
            time_bits.append(str(chosen.get("published_ksa")))
        if chosen.get("publisher"):
            time_bits.append("المصدر: " + str(chosen.get("publisher")))
        if time_bits:
            context_note = (context_note + " " + " | ".join(time_bits)).strip()
        elif chosen.get("title"):
            context_note = (context_note + " وقت الخبر غير متوفر؛ لا نعطيه وزنًا قويًا بدون تاريخ واضح.").strip()
        if fallback_context_used and context_note:
            if chosen_scope == "company":
                context_note += " لا يُحسب كمحفز قوي بسبب ضعف الأثر أو انخفاض الحداثة العملية."
            else:
                context_note += " لا يُحسب كمحفز لأنه خبر سياقي/محايد."

        diag.update({
            "status": "selected_actionable" if best_actionable else "selected_context_fallback",
            "selected_title": title_to_show,
            "selected_scope": chosen.get("scope", ""),
            "selected_sentiment": chosen.get("sentiment", ""),
            "selected_context_only": bool(context_only_news),
            "selected_effect": catalyst_score,
            "fallback_context_used": fallback_context_used,
        })
        bundle.update({
            "news_note": note_to_show,
            "news_title": title_to_show,
            "news_badge": badge_to_show,
            "news_category": chosen.get("category", "neutral"),
            "news_sentiment": chosen.get("sentiment", "neutral"),
            "news_scope": chosen.get("scope", "neutral"),
            "news_scope_label": chosen.get("scope_label", news_scope_label("neutral")),
            "news_freshness_label": chosen.get("freshness_label", ""),
            "news_published_utc": chosen.get("published_utc", ""),
            "news_sessions_since": chosen.get("sessions_since", 999),
            "news_effect_score": catalyst_score,
            "news_is_catalyst": is_catalyst,
            "news_context_note": context_note,
            "news_public_summary": chosen.get("public_news_summary") or build_public_news_summary(chosen.get("scope", "neutral"), chosen.get("sentiment", "neutral"), chosen.get("title", "")),
            "news_context_only": bool(context_only_news or fallback_context_used),
            "news_related_tickers_count": int(chosen.get("related_count", 0) or 0),
            "news_published_ksa": chosen.get("published_ksa", ""),
            "news_age_label": chosen.get("age_label", ""),
            "news_source_name": chosen.get("publisher", ""),
            "news_ai": chosen.get("ai_news", {}),
            "catalyst_score": catalyst_score,
            "news_debug": diag,
        })
        _record_news_diag(symbol, diag)
        return _cache_set(NEWS_CACHE, cache_key, bundle, NEWS_CACHE_TTL_SECONDS)
    except Exception as exc:
        diag["status"] = "error"
        diag["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        bundle["news_debug"] = diag
        _record_news_diag(symbol, diag)
    return bundle


def get_news(symbol, company_name="", sector="", industry=""):
    bundle = get_news_bundle(symbol, company_name, sector, industry)
    return (bundle.get("news_title") or bundle.get("news_context_note") or bundle.get("news_note") or "لا يوجد خبر حديث"), bundle.get("catalyst_score", 0)



