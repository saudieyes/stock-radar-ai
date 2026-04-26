from .settings import NEWS_SCOPE_LABELS, POLYGON_API_KEY
from .utils import *
from .market_data import http_get_json

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
    return int(sessions_since or 999) <= limit


def classify_news_freshness_label(sessions_since: int) -> tuple[str, int]:
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
        "opens", "opening of", "opening", "participate in", "participates in", "conference", "summit", "presents at", "presentation"
    ]
    return any(k in text_lower for k in event_markers)


def detect_news_sentiment(text_lower: str, related_count: int = 0) -> str:
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
        "weak earnings", "missed estimates", "insider sold", "shareholder alert", "steps down", "step down"
    ]
    offering_negative = [
        "public offering", "pricing of public offering", "registered direct offering", "secondary offering",
        "underwritten offering", "pricing of common stock", "prices public offering", "follow-on offering"
    ]
    positive_keywords = [
        "beat", "beats", "strong guidance", "raises guidance", "buyback", "surge", "jumps", "soars",
        "wins", "upgrade", "partnership", "contract", "record revenue", "secures", "launch",
        "breakthrough", "approval", "expands", "growth", "record", "tops estimates", "award",
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
        return "negative" if negative_hits >= positive_hits else "positive"
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

    if related_count >= 2 and not direct_event:
        if sector_hit:
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
    if sector_hit:
        return "sector"
    if symbol_hit and not market_hit and related_count <= 1:
        return "company"
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
        return "⚪ خبر شركة محايد"
    if scope == "sector":
        if sentiment == "positive":
            return "🏭 خبر قطاعي داعم"
        if sentiment in {"negative", "legal"}:
            return "🏭 خبر قطاعي ضاغط"
        return "🏭 سياق قطاعي"
    if scope == "market":
        return "📰 سياق سوق عام"
    if scope in {"opinion", "unrelated", "neutral"}:
        return ""
    return ""


def build_news_context_note(scope: str, sentiment: str, freshness_label: str, effect_score: int, related_count: int = 0) -> str:
    freshness = f" ({freshness_label})" if freshness_label else ""
    oldish = freshness_label in {"أقدم قليلًا", "قديم", "قديم جدًا"}
    stale = freshness_label in {"قديم", "قديم جدًا"}
    if scope == "company":
        if sentiment == "positive":
            if stale or effect_score <= 0:
                return f"هذا خبر يخص الشركة فعلًا لكنه لم يعد حديثًا بما يكفي ليكون محفزًا قويًا الآن{freshness}."
            if oldish:
                return f"خبر حقيقي خاص بالشركة لكنه أقدم قليلًا، لذلك يُستخدم كدعم خفيف لا كمحفز ساخن{freshness}."
            return f"خبر حقيقي خاص بالشركة وذو أثر مباشر داعم للفكرة{freshness}."
        if sentiment == "legal":
            return f"خبر قانوني مباشر على الشركة ويجب اعتباره عامل ضغط واضح على السهم{freshness}."
        if sentiment == "negative":
            return f"خبر سلبي مباشر على الشركة ويخصم من الفكرة الأساسية{freshness}."
        return f"خبر يخص الشركة لكنه لا يقدم محفزًا واضحًا الآن{freshness}."
    if scope == "sector":
        if sentiment == "positive":
            if stale or effect_score <= 0:
                return f"هذا خبر قطاعي قديم أو ضعيف الأثر، لذلك لا يُعامل كمحفز مباشر للفرصة الآن{freshness}."
            if oldish:
                return f"خبر قطاعي داعم لكنه أقدم قليلًا، لذلك وزنه أخف من المعتاد{freshness}."
            return f"خبر قطاعي داعم يفيد السهم بصورة غير مباشرة وبوزن أخف من خبر الشركة{freshness}."
        if sentiment in {"negative", "legal"}:
            return f"خبر قطاعي ضاغط يؤثر على السهم كجزء من القطاع وليس كمحفز خاص بالشركة{freshness}."
        return f"هذا خبر أو سياق قطاعي يفيد الفهم العام لكنه ليس محفزًا مباشرًا للسهم{freshness}."
    if scope == "market":
        if effect_score < 0:
            return f"هذا خبر سوق عام ضاغط يصف المؤشرات أو النفط أو الفيدرالي، ويُعرض كسياق فقط وليس محفزًا مباشرًا للسهم{freshness}."
        if effect_score > 0:
            return f"هذا خبر سوق عام داعم بصورة خفيفة جدًا، ويُعرض كسياق عام لا كمحفز خاص بالشركة{freshness}."
        return f"هذا خبر سوق عام محايد يُعرض كسياق ولا يضيف نقاطًا مباشرة للسهم{freshness}."
    if scope == "opinion":
        if related_count >= 2:
            return "هذا محتوى تجميعي/مقال رأي يذكر عدة شركات، لذلك لا يُعامل كخبر محفز مباشر ولا يضيف نقاطًا."
        return "لا يوجد خبر محفز معتمد الآن؛ الموجود مجرد مقال رأي أو تحليل عام لا نستخدمه كمحفز."
    if scope == "unrelated":
        return "الخبر يذكر السهم ضمن قائمة أو سياق عام، لكنه ليس خبرًا مباشرًا نعتمد عليه كمحفز."
    return "لا يوجد خبر أو محفز حديث يمكن الاعتماد عليه الآن."


def get_news_bundle(symbol, company_name="", sector="", industry=""):
    bundle = {
        "news_note": "لا يوجد خبر أو محفز حديث",
        "news_title": "",
        "news_badge": "",
        "news_category": "neutral",
        "news_sentiment": "neutral",
        "news_scope": "neutral",
        "news_scope_label": news_scope_label("neutral"),
        "news_freshness_label": "",
        "news_published_utc": "",
        "news_sessions_since": 999,
        "news_effect_score": 0,
        "news_is_catalyst": False,
        "news_context_note": "لا يوجد خبر أو محفز حديث يمكن الاعتماد عليه الآن.",
        "news_related_tickers_count": 0,
        "catalyst_score": 0,
    }
    try:
        url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&order=desc&sort=published_utc&apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        results = r.get("results", [])
        if not results:
            return bundle

        company_variants = get_company_name_variants(company_name)
        sector_variants = get_sector_name_variants(sector, industry)
        best = None
        best_score = -9999

        for item in results:
            title = str(item.get("title", "") or "").strip()
            desc = str(item.get("description", "") or item.get("summary", "") or "").strip()
            if not title:
                continue

            full_text = normalize_text(f"{title} {desc}")
            related = [str(x).upper().strip() for x in item.get("tickers", []) if str(x).strip()]
            related_count = len(related)
            published_utc = str(item.get("published_utc", "") or "")
            sessions_since = trading_sessions_since_news(published_utc)
            freshness_label, freshness_score = classify_news_freshness_label(sessions_since)
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

            scope_priority = {
                "company": 64,
                "sector": 32,
                "market": 14,
                "neutral": 4,
                "opinion": -24,
                "unrelated": -26,
            }.get(scope, 0)
            sentiment_bonus = {
                "positive": 3,
                "negative": 5,
                "legal": 10,
                "neutral": 0,
                "opinion": -10,
            }.get(sentiment, 0)
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
                "impact": effect,
                "badge": badge,
                "scope": scope,
                "scope_label": news_scope_label(scope),
                "category": "neutral" if sentiment == "opinion" else sentiment,
                "sentiment": sentiment,
                "freshness_label": freshness_label,
                "published_utc": published_utc,
                "sessions_since": sessions_since,
                "context_note": context_note,
                "related_count": related_count,
            }

            hard_excluded = False
            if news_shape in {"opinion", "roundup"}:
                hard_excluded = True
            if "transcript" in full_text or "prepared remarks" in full_text:
                hard_excluded = True
            if not is_news_within_session_limit(scope, sentiment, sessions_since):
                hard_excluded = True
            if scope not in {"company", "sector"}:
                hard_excluded = True
            if sentiment not in {"positive", "negative", "legal"}:
                hard_excluded = True
            if scope == "sector" and news_shape != "direct":
                hard_excluded = True
            if scope == "company" and related_count >= 2 and not direct_event:
                hard_excluded = True
            if candidate_score <= 0:
                hard_excluded = True

            if hard_excluded:
                continue

            if candidate_score > best_score:
                best_score = candidate_score
                best = candidate

        chosen = best
        if not chosen:
            return bundle

        title_to_show = chosen["title"]
        note_to_show = title_to_show or chosen.get("context_note", "")
        badge_to_show = chosen.get("badge", "")
        if chosen.get("scope") in {"opinion", "unrelated", "neutral", "market"}:
            title_to_show = ""
            badge_to_show = ""
            note_to_show = chosen.get("context_note", "") or "لا يوجد خبر أو محفز حديث"

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
            "news_effect_score": int(chosen.get("impact", 0) or 0),
            "news_is_catalyst": chosen.get("scope") in {"company", "sector"} and int(chosen.get("impact", 0) or 0) != 0,
            "news_context_note": chosen.get("context_note", ""),
            "news_related_tickers_count": int(chosen.get("related_count", 0) or 0),
            "catalyst_score": int(chosen.get("impact", 0) or 0),
        })
    except:
        pass
    return bundle


def get_news(symbol, company_name="", sector="", industry=""):
    bundle = get_news_bundle(symbol, company_name, sector, industry)
    return (bundle.get("news_title") or bundle.get("news_context_note") or bundle.get("news_note") or "لا يوجد خبر حديث"), bundle.get("catalyst_score", 0)


