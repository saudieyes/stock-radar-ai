"""AI news classification helper for Stock Radar AI.

This layer classifies news only. It never makes trading decisions. The news engine
uses the result defensively: AI can block/remove catalyst points for opinion,
unrelated, mixed, low-confidence, or low-materiality items.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import re

from .settings import (
    ANTHROPIC_API_KEY,
    AI_NEWS_CACHE_TTL_SECONDS,
    AI_NEWS_ENABLED,
    AI_NEWS_MIN_CONFIDENCE,
    AI_NEWS_MODEL,
    AI_NEWS_PROVIDER,
    AI_NEWS_TIMEOUT_SEC,
    HTTP_SESSION,
)
from .utils import _cache_get, _cache_set, normalize_symbol_text

AI_NEWS_CACHE: dict = {}


def get_ai_news_status() -> dict:
    """Safe diagnostics. Never exposes the API key."""
    try:
        return {
            "ok": True,
            "enabled": bool(AI_NEWS_ENABLED),
            "provider": AI_NEWS_PROVIDER,
            "model": AI_NEWS_MODEL,
            "has_anthropic_key": bool(ANTHROPIC_API_KEY),
            "cache_size": len(AI_NEWS_CACHE),
            "min_confidence": int(AI_NEWS_MIN_CONFIDENCE or 70),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def format_news_time_labels(published_utc: str) -> tuple[str, str]:
    try:
        if not published_utc:
            return "", ""
        dt = datetime.fromisoformat(str(published_utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ksa = dt.astimezone(ZoneInfo("Asia/Riyadh"))
        now_utc = datetime.now(timezone.utc)
        age_seconds = max(0, int((now_utc - dt.astimezone(timezone.utc)).total_seconds()))
        if age_seconds < 3600:
            age_label = f"منذ {max(1, age_seconds // 60)} دقيقة"
        elif age_seconds < 86400:
            age_label = f"منذ {max(1, age_seconds // 3600)} ساعة"
        else:
            age_label = f"منذ {max(1, age_seconds // 86400)} يوم"
        return ksa.strftime("%Y-%m-%d %H:%M KSA"), age_label
    except Exception:
        return "", ""


def _extract_json_object(text: str) -> dict | None:
    try:
        raw = str(text or "").strip().strip("` \n\t")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return None
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _choice(value: str, allowed: set[str], default: str) -> str:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in allowed else default


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _reason(text: str) -> str:
    txt = str(text or "").strip()
    if len(txt) > 220:
        txt = txt[:220].rstrip() + "…"
    return txt


def classify_news_with_ai(symbol: str, company_name: str, sector: str, industry: str, candidate: dict) -> dict | None:
    """Classify selected candidate news through Claude. Returns normalized dict or None."""
    try:
        if not (AI_NEWS_ENABLED and AI_NEWS_PROVIDER == "claude" and ANTHROPIC_API_KEY):
            return None
        title = str(candidate.get("title", "") or "").strip()
        if not title:
            return None

        raw_key = "|".join([
            str(AI_NEWS_MODEL),
            normalize_symbol_text(symbol),
            title[:240],
            str(candidate.get("published_utc", "") or ""),
            str(candidate.get("description", "") or "")[:300],
        ])
        cache_key = hashlib.sha1(raw_key.encode("utf-8", errors="ignore")).hexdigest()
        cached = _cache_get(AI_NEWS_CACHE, cache_key)
        if isinstance(cached, dict):
            out = dict(cached)
            out["cache_hit"] = True
            return out

        payload = {
            "task": "Classify a news item for a stock radar. Do not give trading advice.",
            "stock": {
                "symbol": normalize_symbol_text(symbol),
                "company": company_name or "",
                "sector": sector or "",
                "industry": industry or "",
            },
            "news": {
                "title": title,
                "description": str(candidate.get("description", "") or "")[:900],
                "published_utc": str(candidate.get("published_utc", "") or ""),
                "publisher": str(candidate.get("publisher", "") or ""),
                "related_tickers": candidate.get("related_tickers", []) or [],
            },
            "rule_precheck": {
                "scope": candidate.get("scope", "neutral"),
                "sentiment": candidate.get("sentiment", "neutral"),
                "shape": candidate.get("shape", ""),
                "sessions_since": candidate.get("sessions_since", 999),
            },
            "strict_rules": [
                "Stock-picking/opinion/listicle articles such as buy/hold forever, should you buy/avoid, best stocks: scope=opinion, is_opinion=true, catalyst_allowed=false.",
                "If the article is about another company and not this stock: scope=unrelated, catalyst_allowed=false.",
                "If the title directly mentions this symbol/company and describes a real event: prefer scope=company over sector.",
                "Do not classify as sector if stock sector/industry is unknown or the sector relationship is speculative.",
                "Sector and market news are context-only: catalyst_allowed=false even when positive.",
                "Mixed earnings news should be sentiment=mixed and catalyst_allowed=false unless clearly material negative.",
                "Only direct, recent, material company events can have catalyst_allowed=true.",
                "The reason must be concise Arabic only, maximum one short sentence.",
            ],
            "required_json_schema": {
                "scope": "company|sector|market|opinion|unrelated|neutral",
                "sentiment": "positive|negative|mixed|neutral|legal",
                "materiality": "high|medium|low",
                "is_opinion": "boolean",
                "is_direct_company_news": "boolean",
                "catalyst_allowed": "boolean",
                "confidence": "0-100 integer",
                "reason": "short Arabic reason"
            },
        }

        body = {
            "model": AI_NEWS_MODEL,
            "max_tokens": 420,
            "temperature": 0,
            "system": "You are a strict financial-news classifier. Return only valid JSON. No markdown. The reason field must be concise Arabic only.",
            "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        }
        resp = HTTP_SESSION.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=float(AI_NEWS_TIMEOUT_SEC or 8.0),
        )
        if resp.status_code >= 300:
            return {"ok": False, "error": f"anthropic_http_{resp.status_code}: {resp.text[:180]}"}
        data = resp.json()
        parts = data.get("content", []) if isinstance(data, dict) else []
        text = "\n".join(str(x.get("text", "") or "") for x in parts if isinstance(x, dict))
        obj = _extract_json_object(text)
        if not obj:
            return {"ok": False, "error": "invalid_ai_json"}
        out = {
            "ok": True,
            "scope": _choice(obj.get("scope"), {"company", "sector", "market", "opinion", "unrelated", "neutral"}, "neutral"),
            "sentiment": _choice(obj.get("sentiment"), {"positive", "negative", "mixed", "neutral", "legal"}, "neutral"),
            "materiality": _choice(obj.get("materiality"), {"high", "medium", "low"}, "low"),
            "is_opinion": _truthy(obj.get("is_opinion")),
            "is_direct_company_news": _truthy(obj.get("is_direct_company_news")),
            "catalyst_allowed": _truthy(obj.get("catalyst_allowed")),
            "confidence": int(max(0, min(100, float(obj.get("confidence", 0) or 0)))),
            "reason": _reason(obj.get("reason", "")),
            "cache_hit": False,
        }
        return _cache_set(AI_NEWS_CACHE, cache_key, out, AI_NEWS_CACHE_TTL_SECONDS)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


