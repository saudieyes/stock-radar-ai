"""SEC XBRL based Sharia financial evidence store.

V2W19a: SEC becomes the primary financial evidence source for the Sharia
screen.  The expensive 1GB+ companyfacts.zip is imported on demand into the
existing SQLite database; live scans only read compact local tables.
"""
from __future__ import annotations

import json
import math
import os
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .settings import (
    DATA_DIR,
    SHARIA_GRAY_CASH_TO_ASSETS,
    SHARIA_MAX_DEBT_TO_ASSETS,
    SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE,
)
from .sqlite_store import SQLITE_ENABLED, _connect, init_db
from .utils import normalize_symbol_text, safe_round

SEC_SHARIA_VERSION = "sec_xbrl_sharia_primary_v2w19b_admin_loader_2026_06_28"

SEC_DIR = Path(os.getenv("SEC_SHARIA_DATA_DIR", str(DATA_DIR / "sec")) or str(DATA_DIR / "sec"))
SEC_COMPANYFACTS_ZIP = Path(os.getenv("SEC_COMPANYFACTS_ZIP", str(SEC_DIR / "companyfacts.zip")) or str(SEC_DIR / "companyfacts.zip"))
SEC_TICKERS_EXCHANGE_JSON = Path(os.getenv("SEC_TICKERS_EXCHANGE_JSON", str(SEC_DIR / "company_tickers_exchange.json")) or str(SEC_DIR / "company_tickers_exchange.json"))
SEC_SHARIA_ACTIVE_FLAG = Path(os.getenv("SEC_SHARIA_ACTIVE_FLAG", str(SEC_DIR / "sec_sharia_active.json")) or str(SEC_DIR / "sec_sharia_active.json"))

# Keep this configurable.  SEC company facts are official, but if the imported
# database is not refreshed for a long time, we must not keep calling old facts clean.
SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS = int(float(os.getenv("SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS", "540") or 540))
SHARIA_SEC_PRIMARY_ENABLED = str(os.getenv("SHARIA_SEC_PRIMARY_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
SHARIA_SEC_FORCE_ACTIVE = str(os.getenv("SHARIA_SEC_FORCE_ACTIVE", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}

_BALANCE_FORMS = {"10-Q", "10-K", "20-F", "40-F", "6-K"}
_DURATION_FORMS = {"10-Q", "10-K", "20-F", "40-F", "6-K"}

ASSET_TAGS = [
    "Assets",
    "AssetsCurrent",
]
DEBT_TAGS_DIRECT = [
    "DebtAndFinanceLeaseObligations",
    "DebtCurrent",
    "DebtNoncurrent",
    "LongTermDebt",
    "LongTermDebtAndFinanceLeaseObligations",
]
DEBT_CURRENT_TAGS = [
    "DebtCurrent",
    "ShortTermBorrowings",
    "ShortTermDebt",
    "ShortTermDebtAndCurrentMaturitiesOfLongTermDebt",
    "CurrentMaturitiesOfLongTermDebt",
    "CurrentPortionOfLongTermDebt",
    "CurrentPortionOfLongTermDebtAndFinanceLeaseObligations",
]
DEBT_NONCURRENT_TAGS = [
    "LongTermDebtNoncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    "LongTermDebt",
    "FinanceLeaseLiabilityNoncurrent",
]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "Cash",
]
INVESTMENT_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
]
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
INTEREST_EXPENSE_TAGS = [
    "InterestExpenseNonOperating",
    "InterestExpense",
    "InterestExpenseDebt",
    "InterestAndDebtExpense",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _days_old(value: Any) -> int | None:
    d = _parse_date(value)
    if not d:
        return None
    return max(0, (date.today() - d).days)


def _float_value(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _clean_symbol(symbol: str) -> str:
    return normalize_symbol_text(str(symbol or "").replace(".", "-")).upper().strip()


def _cik10(cik: Any) -> str:
    digits = "".join(ch for ch in str(cik or "") if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(10)



def _load_active_flag() -> dict:
    try:
        if SEC_SHARIA_ACTIVE_FLAG.exists():
            with open(SEC_SHARIA_ACTIVE_FLAG, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    return {}


def is_sec_sharia_ready() -> bool:
    """Return True only after the admin/full importer has activated SEC primary.

    This makes V2W19b safe to deploy: before the one-click setup completes,
    the old local financial filter continues running instead of turning every
    symbol into SEC-missing/gray.  After a successful full import, the admin
    loader writes SEC_SHARIA_ACTIVE_FLAG and SEC becomes primary.
    """
    if SHARIA_SEC_FORCE_ACTIVE:
        return True
    flag = _load_active_flag()
    return bool(flag.get("active") is True)


def mark_sec_sharia_active(mode: str = "full", details: dict | None = None) -> dict:
    SEC_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": True,
        "mode": str(mode or "full"),
        "activated_at": _now_iso(),
        "version": SEC_SHARIA_VERSION,
        "details": details or {},
    }
    try:
        with open(SEC_SHARIA_ACTIVE_FLAG, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"ok": True, "path": str(SEC_SHARIA_ACTIVE_FLAG), "payload": payload}
    except Exception as exc:
        return {"ok": False, "path": str(SEC_SHARIA_ACTIVE_FLAG), "error": f"{type(exc).__name__}: {str(exc)[:220]}"}


def init_sec_sharia_db() -> None:
    if not SQLITE_ENABLED:
        return
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_company_map (
                symbol TEXT PRIMARY KEY,
                cik TEXT NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_latest_financials (
                symbol TEXT PRIMARY KEY,
                cik TEXT NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                filing_date TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL DEFAULT '',
                form TEXT NOT NULL DEFAULT '',
                assets REAL NOT NULL DEFAULT 0,
                total_debt REAL NOT NULL DEFAULT 0,
                cash_and_equivalents REAL NOT NULL DEFAULT 0,
                short_term_investments REAL NOT NULL DEFAULT 0,
                revenue REAL NOT NULL DEFAULT 0,
                interest_expense REAL NOT NULL DEFAULT 0,
                fact_age_days INTEGER,
                source TEXT NOT NULL DEFAULT 'SEC',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sharia_screen_results (
                symbol TEXT PRIMARY KEY,
                final_status TEXT NOT NULL DEFAULT 'sec_missing_data',
                label TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                evidence_source TEXT NOT NULL DEFAULT 'SEC',
                evidence_age_days INTEGER,
                debt_ratio REAL,
                cash_ratio REAL,
                interest_ratio REAL,
                filing_date TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_company_cik ON sec_company_map(cik)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sharia_final_status ON sharia_screen_results(final_status)")
        conn.commit()


def _load_ticker_rows(path: Path = SEC_TICKERS_EXCHANGE_JSON) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows: list[dict] = []
    if isinstance(payload, dict) and isinstance(payload.get("fields"), list) and isinstance(payload.get("data"), list):
        fields = [str(x) for x in payload.get("fields")]
        for raw in payload.get("data") or []:
            if isinstance(raw, list):
                rows.append({fields[i]: raw[i] if i < len(raw) else None for i in range(len(fields))})
    elif isinstance(payload, dict):
        for item in payload.values():
            if isinstance(item, dict):
                rows.append(item)
    elif isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    return rows


def import_sec_company_map(path: Path = SEC_TICKERS_EXCHANGE_JSON) -> dict:
    init_sec_sharia_db()
    out = {"ok": False, "path": str(path), "rows": 0, "inserted": 0, "error": ""}
    if not path.exists():
        out["error"] = "company_tickers_exchange.json not found"
        return out
    try:
        rows = _load_ticker_rows(path)
        now = _now_iso()
        inserted = 0
        with _connect() as conn:
            for r in rows:
                sym = _clean_symbol(r.get("ticker") or r.get("symbol") or "")
                cik = _cik10(r.get("cik") or r.get("CIK") or "")
                if not sym or not cik:
                    continue
                conn.execute(
                    """
                    INSERT INTO sec_company_map(symbol, cik, company_name, exchange, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        cik=excluded.cik,
                        company_name=excluded.company_name,
                        exchange=excluded.exchange,
                        updated_at=excluded.updated_at
                    """,
                    (sym, cik, str(r.get("name") or r.get("title") or "")[:300], str(r.get("exchange") or "")[:80], now),
                )
                inserted += 1
            conn.commit()
        out.update({"ok": True, "rows": len(rows), "inserted": inserted})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return out


def _iter_symbol_ciks(symbols: Iterable[str] | None = None) -> list[dict]:
    init_sec_sharia_db()
    wanted = [_clean_symbol(s) for s in (symbols or []) if _clean_symbol(s)]
    with _connect() as conn:
        if wanted:
            placeholders = ",".join(["?"] * len(wanted))
            rows = conn.execute(f"SELECT * FROM sec_company_map WHERE symbol IN ({placeholders})", wanted).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sec_company_map ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


def _unit_facts(facts: dict, tag: str) -> list[dict]:
    node = ((facts or {}).get("us-gaap") or {}).get(tag) or {}
    units = node.get("units") or {}
    rows: list[dict] = []
    for unit_name in ("USD", "USD/shares", "shares"):
        arr = units.get(unit_name)
        if isinstance(arr, list):
            rows.extend([dict(x, _tag=tag, _unit=unit_name) for x in arr if isinstance(x, dict)])
    # Financial values almost always use USD.  If no USD was found, accept the
    # first available unit as a low-confidence fallback rather than missing all ADRs.
    if not rows:
        for unit_name, arr in units.items():
            if isinstance(arr, list):
                rows.extend([dict(x, _tag=tag, _unit=str(unit_name)) for x in arr if isinstance(x, dict)])
                break
    return rows


def _candidate_score(row: dict, *, duration: bool) -> tuple:
    filed = _parse_date(row.get("filed")) or date.min
    end = _parse_date(row.get("end")) or date.min
    form = str(row.get("form") or "")
    form_bonus = 1 if form in (_DURATION_FORMS if duration else _BALANCE_FORMS) else 0
    fp = str(row.get("fp") or "")
    fp_bonus = 1 if fp in {"Q1", "Q2", "Q3", "FY"} else 0
    return (filed, end, form_bonus, fp_bonus)


def _latest_fact(facts: dict, tags: list[str], *, duration: bool = False) -> dict | None:
    candidates: list[dict] = []
    for tag in tags:
        for row in _unit_facts(facts, tag):
            val = _float_value(row.get("val"))
            if val is None:
                continue
            if str(row.get("form") or "") and str(row.get("form") or "") not in (_DURATION_FORMS if duration else _BALANCE_FORMS):
                # Keep 8-K/other forms out of the normal financial screen.
                continue
            r = dict(row)
            r["val"] = val
            candidates.append(r)
    if not candidates:
        return None
    candidates.sort(key=lambda r: _candidate_score(r, duration=duration), reverse=True)
    return candidates[0]


def _latest_sum(facts: dict, tags: list[str], *, duration: bool = False) -> tuple[float, dict | None]:
    # Sum the latest value of each debt component.  This avoids missing total debt
    # when the issuer reports current and non-current debt separately.
    total = 0.0
    best_row = None
    used_tags = set()
    for tag in tags:
        row = _latest_fact(facts, [tag], duration=duration)
        if not row:
            continue
        used_tags.add(tag)
        total += max(0.0, float(row.get("val") or 0))
        if best_row is None or _candidate_score(row, duration=duration) > _candidate_score(best_row, duration=duration):
            best_row = row
    if best_row is not None:
        best_row = dict(best_row)
        best_row["_used_tags"] = sorted(used_tags)
    return total, best_row


def _extract_financials_from_companyfacts(payload: dict, symbol: str, cik: str = "", company_name: str = "") -> dict:
    facts = payload.get("facts") or {}
    entity = str(payload.get("entityName") or company_name or "")

    assets_row = _latest_fact(facts, ASSET_TAGS)
    cash_row = _latest_fact(facts, CASH_TAGS)
    investments_row = _latest_fact(facts, INVESTMENT_TAGS)
    revenue_row = _latest_fact(facts, REVENUE_TAGS, duration=True)
    interest_row = _latest_fact(facts, INTEREST_EXPENSE_TAGS, duration=True)

    direct_debt, debt_row_direct = _latest_sum(facts, DEBT_TAGS_DIRECT)
    current_debt, current_row = _latest_sum(facts, DEBT_CURRENT_TAGS)
    noncurrent_debt, noncurrent_row = _latest_sum(facts, DEBT_NONCURRENT_TAGS)
    component_debt = max(0.0, current_debt) + max(0.0, noncurrent_debt)
    total_debt = max(direct_debt, component_debt)
    debt_row = debt_row_direct or current_row or noncurrent_row

    latest_rows = [x for x in [assets_row, debt_row, cash_row, investments_row, revenue_row, interest_row] if x]
    latest_rows.sort(key=lambda r: _candidate_score(r, duration=False), reverse=True)
    anchor = assets_row or (latest_rows[0] if latest_rows else {})

    assets = max(0.0, float((assets_row or {}).get("val") or 0))
    cash = max(0.0, float((cash_row or {}).get("val") or 0))
    investments = max(0.0, float((investments_row or {}).get("val") or 0))
    revenue = max(0.0, float((revenue_row or {}).get("val") or 0))
    interest = abs(float((interest_row or {}).get("val") or 0))
    filed = str((anchor or {}).get("filed") or "")
    period_end = str((anchor or {}).get("end") or "")
    form = str((anchor or {}).get("form") or "")

    return {
        "symbol": _clean_symbol(symbol),
        "cik": _cik10(cik or payload.get("cik") or ""),
        "company_name": entity,
        "filing_date": filed,
        "period_end": period_end,
        "form": form,
        "assets": assets,
        "total_debt": total_debt,
        "cash_and_equivalents": cash,
        "short_term_investments": investments,
        "revenue": revenue,
        "interest_expense": interest,
        "fact_age_days": _days_old(filed),
        "asset_tag": (assets_row or {}).get("_tag", ""),
        "debt_tags": ",".join((debt_row or {}).get("_used_tags", []) or [str((debt_row or {}).get("_tag", ""))]),
        "cash_tag": (cash_row or {}).get("_tag", ""),
        "revenue_tag": (revenue_row or {}).get("_tag", ""),
        "interest_tag": (interest_row or {}).get("_tag", ""),
    }


def _screen_financials(row: dict) -> dict:
    symbol = _clean_symbol(row.get("symbol") or "")
    assets = float(row.get("assets") or 0)
    debt = float(row.get("total_debt") or 0)
    cash = float(row.get("cash_and_equivalents") or 0) + float(row.get("short_term_investments") or 0)
    revenue = float(row.get("revenue") or 0)
    interest = abs(float(row.get("interest_expense") or 0))
    evidence_age = row.get("fact_age_days")
    try:
        evidence_age_i = int(evidence_age) if evidence_age is not None else None
    except Exception:
        evidence_age_i = None

    if assets <= 0:
        return {
            "symbol": symbol,
            "final_status": "sec_missing_data",
            "label": "رمادي — بيانات SEC ناقصة",
            "reason": "لا توجد أصول حديثة قابلة للاستخدام من SEC XBRL؛ لا يعتمد عليه كمتوافق حتى تكتمل البيانات.",
            "evidence_age_days": evidence_age_i,
            "debt_ratio": None,
            "cash_ratio": None,
            "interest_ratio": None,
        }

    if evidence_age_i is None or evidence_age_i > SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS:
        return {
            "symbol": symbol,
            "final_status": "sec_stale_data",
            "label": "رمادي — بيانات SEC قديمة",
            "reason": f"آخر دليل SEC أقدم من الحد المسموح ({SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS} يومًا)، لذلك لا يعطي clean.",
            "evidence_age_days": evidence_age_i,
            "debt_ratio": None,
            "cash_ratio": None,
            "interest_ratio": None,
        }

    debt_ratio = debt / assets if assets > 0 else 0.0
    cash_ratio = cash / assets if assets > 0 else 0.0
    interest_ratio = interest / revenue if revenue > 0 and interest > 0 else 0.0

    if debt_ratio > float(SHARIA_MAX_DEBT_TO_ASSETS or 0.33):
        return {
            "symbol": symbol,
            "final_status": "sec_blocked_financial",
            "label": "غير متوافق ماليًا — SEC",
            "reason": f"SEC: الديون/الأصول {safe_round(debt_ratio * 100, 1)}% وتتجاوز الحد {safe_round(float(SHARIA_MAX_DEBT_TO_ASSETS or 0.33) * 100, 1)}%.",
            "evidence_age_days": evidence_age_i,
            "debt_ratio": safe_round(debt_ratio, 4),
            "cash_ratio": safe_round(cash_ratio, 4),
            "interest_ratio": safe_round(interest_ratio, 4),
        }
    if interest_ratio > float(SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE or 0.05):
        return {
            "symbol": symbol,
            "final_status": "sec_blocked_financial",
            "label": "غير متوافق ماليًا — SEC",
            "reason": f"SEC: مصروف الفوائد/الإيرادات {safe_round(interest_ratio * 100, 1)}% ويتجاوز الحد {safe_round(float(SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE or 0.05) * 100, 1)}%.",
            "evidence_age_days": evidence_age_i,
            "debt_ratio": safe_round(debt_ratio, 4),
            "cash_ratio": safe_round(cash_ratio, 4),
            "interest_ratio": safe_round(interest_ratio, 4),
        }
    if cash_ratio > float(SHARIA_GRAY_CASH_TO_ASSETS or 0.33):
        return {
            "symbol": symbol,
            "final_status": "sec_needs_review",
            "label": "رمادي — SEC يحتاج مراجعة",
            "reason": f"SEC: النقد والاستثمارات السائلة/الأصول {safe_round(cash_ratio * 100, 1)}%؛ لا يدخل Strong/Cautious قبل مراجعة.",
            "evidence_age_days": evidence_age_i,
            "debt_ratio": safe_round(debt_ratio, 4),
            "cash_ratio": safe_round(cash_ratio, 4),
            "interest_ratio": safe_round(interest_ratio, 4),
        }
    return {
        "symbol": symbol,
        "final_status": "sec_clean",
        "label": "متوافق مبدئيًا — SEC",
        "reason": f"SEC: مطابق للضوابط المالية المبدئية؛ الديون {safe_round(debt_ratio * 100, 1)}%، الفوائد {safe_round(interest_ratio * 100, 1)}%.",
        "evidence_age_days": evidence_age_i,
        "debt_ratio": safe_round(debt_ratio, 4),
        "cash_ratio": safe_round(cash_ratio, 4),
        "interest_ratio": safe_round(interest_ratio, 4),
    }


def _upsert_financial_and_screen(row: dict) -> None:
    now = _now_iso()
    screen = _screen_financials(row)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sec_latest_financials(
                symbol, cik, company_name, filing_date, period_end, form,
                assets, total_debt, cash_and_equivalents, short_term_investments,
                revenue, interest_expense, fact_age_days, source, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SEC', ?)
            ON CONFLICT(symbol) DO UPDATE SET
                cik=excluded.cik,
                company_name=excluded.company_name,
                filing_date=excluded.filing_date,
                period_end=excluded.period_end,
                form=excluded.form,
                assets=excluded.assets,
                total_debt=excluded.total_debt,
                cash_and_equivalents=excluded.cash_and_equivalents,
                short_term_investments=excluded.short_term_investments,
                revenue=excluded.revenue,
                interest_expense=excluded.interest_expense,
                fact_age_days=excluded.fact_age_days,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                row.get("symbol", ""), row.get("cik", ""), row.get("company_name", ""), row.get("filing_date", ""),
                row.get("period_end", ""), row.get("form", ""), float(row.get("assets") or 0), float(row.get("total_debt") or 0),
                float(row.get("cash_and_equivalents") or 0), float(row.get("short_term_investments") or 0),
                float(row.get("revenue") or 0), float(row.get("interest_expense") or 0), row.get("fact_age_days"), now,
            ),
        )
        conn.execute(
            """
            INSERT INTO sharia_screen_results(
                symbol, final_status, label, reason, evidence_source, evidence_age_days,
                debt_ratio, cash_ratio, interest_ratio, filing_date, period_end, updated_at
            ) VALUES(?, ?, ?, ?, 'SEC', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                final_status=excluded.final_status,
                label=excluded.label,
                reason=excluded.reason,
                evidence_source=excluded.evidence_source,
                evidence_age_days=excluded.evidence_age_days,
                debt_ratio=excluded.debt_ratio,
                cash_ratio=excluded.cash_ratio,
                interest_ratio=excluded.interest_ratio,
                filing_date=excluded.filing_date,
                period_end=excluded.period_end,
                updated_at=excluded.updated_at
            """,
            (
                row.get("symbol", ""), screen.get("final_status", "sec_missing_data"), screen.get("label", ""),
                screen.get("reason", ""), screen.get("evidence_age_days"), screen.get("debt_ratio"), screen.get("cash_ratio"),
                screen.get("interest_ratio"), row.get("filing_date", ""), row.get("period_end", ""), now,
            ),
        )
        conn.commit()


def _zip_member_for_cik(zf: zipfile.ZipFile, cik10: str, names_cache: dict[str, str] | None = None) -> str | None:
    candidates = [f"CIK{cik10}.json", f"companyfacts/CIK{cik10}.json", f"{cik10}.json"]
    names = names_cache or {n.rsplit("/", 1)[-1]: n for n in zf.namelist()}
    for c in candidates:
        if c in zf.namelist():
            return c
        base = c.rsplit("/", 1)[-1]
        if base in names:
            return names[base]
    return None


def import_companyfacts_zip(
    facts_zip: Path = SEC_COMPANYFACTS_ZIP,
    *,
    symbols: Iterable[str] | None = None,
    limit: int | None = None,
    progress_every: int = 250,
) -> dict:
    init_sec_sharia_db()
    out = {
        "ok": False,
        "version": SEC_SHARIA_VERSION,
        "facts_zip": str(facts_zip),
        "symbols_requested": len(list(symbols or [])) if symbols else 0,
        "processed": 0,
        "inserted": 0,
        "missing_zip_members": 0,
        "errors": [],
        "started_at": _now_iso(),
        "finished_at": "",
    }
    if not facts_zip.exists():
        out["errors"].append("companyfacts.zip not found")
        out["finished_at"] = _now_iso()
        return out
    rows = _iter_symbol_ciks(symbols)
    if limit and int(limit) > 0:
        rows = rows[: int(limit)]
    try:
        with zipfile.ZipFile(facts_zip, "r") as zf:
            names_cache = {n.rsplit("/", 1)[-1]: n for n in zf.namelist()}
            for idx, m in enumerate(rows, start=1):
                sym = _clean_symbol(m.get("symbol") or "")
                cik = _cik10(m.get("cik") or "")
                if not sym or not cik:
                    continue
                out["processed"] += 1
                member = _zip_member_for_cik(zf, cik, names_cache)
                if not member:
                    out["missing_zip_members"] += 1
                    continue
                try:
                    with zf.open(member) as f:
                        payload = json.load(f)
                    financial = _extract_financials_from_companyfacts(payload, sym, cik=cik, company_name=str(m.get("company_name") or ""))
                    _upsert_financial_and_screen(financial)
                    out["inserted"] += 1
                except Exception as exc:
                    if len(out["errors"]) < 25:
                        out["errors"].append({"symbol": sym, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
                if progress_every and idx % int(progress_every) == 0:
                    # Useful in Railway logs when run from CLI.
                    print(f"SEC_SHARIA_IMPORT_PROGRESS processed={out['processed']} inserted={out['inserted']}", flush=True)
        out["ok"] = True
    except Exception as exc:
        out["errors"].append(f"{type(exc).__name__}: {str(exc)[:240]}")
    out["finished_at"] = _now_iso()
    out["duration_sec"] = safe_round(time.time() - time.mktime(datetime.fromisoformat(out["started_at"].replace("Z", "+00:00")).timetuple()), 1)
    return out


def get_sec_financials(symbol: str) -> dict | None:
    if not SQLITE_ENABLED or not SHARIA_SEC_PRIMARY_ENABLED:
        return None
    sym = _clean_symbol(symbol)
    if not sym:
        return None
    try:
        init_sec_sharia_db()
        with _connect() as conn:
            row = conn.execute("SELECT * FROM sec_latest_financials WHERE symbol=?", (sym,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_sec_screen_result(symbol: str) -> dict | None:
    if not SQLITE_ENABLED or not SHARIA_SEC_PRIMARY_ENABLED:
        return None
    sym = _clean_symbol(symbol)
    if not sym:
        return None
    try:
        init_sec_sharia_db()
        with _connect() as conn:
            row = conn.execute("SELECT * FROM sharia_screen_results WHERE symbol=?", (sym,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def sec_missing_response(symbol: str) -> dict:
    return {
        "symbol": _clean_symbol(symbol),
        "final_status": "sec_missing_data",
        "label": "رمادي — بيانات SEC غير متوفرة",
        "reason": "لم يتم العثور على نتيجة SEC محلية لهذا الرمز؛ لا يعتمد عليه كمتوافق حتى يتم حقن companyfacts.zip.",
        "evidence_source": "SEC",
        "evidence_age_days": None,
        "debt_ratio": None,
        "cash_ratio": None,
        "interest_ratio": None,
    }


def map_sec_result_to_assessment(symbol: str, result: dict | None) -> dict:
    result = result or sec_missing_response(symbol)
    status = str(result.get("final_status") or "sec_missing_data")
    label = str(result.get("label") or "")
    reason = str(result.get("reason") or "")
    base = {
        "status": status,
        "label": label or status,
        "reason": reason,
        "manual_excluded": False,
        "manual_approved": False,
        "note": "",
        "debt_to_assets": result.get("debt_ratio"),
        "interest_expense_to_revenue": result.get("interest_ratio"),
        "cash_to_assets": result.get("cash_ratio"),
        "sharia_evidence_source": "SEC",
        "evidence_source": "SEC",
        "evidence_age_days": result.get("evidence_age_days"),
        "filing_date": result.get("filing_date", ""),
        "period_end": result.get("period_end", ""),
    }
    if status == "sec_clean":
        base.update({"is_gray": False, "is_halal": True, "should_block": False, "source_filter_action": "allow"})
    elif status == "sec_blocked_financial":
        base.update({"is_gray": False, "is_halal": False, "should_block": True, "source_filter_action": "block"})
    else:
        base.update({"is_gray": True, "is_halal": True, "should_block": False, "source_filter_action": "gray"})
    return base


def sec_sharia_status(sample_limit: int = 12) -> dict:
    out = {
        "ok": False,
        "version": SEC_SHARIA_VERSION,
        "sec_primary_enabled": bool(SHARIA_SEC_PRIMARY_ENABLED),
        "sec_primary_ready": bool(is_sec_sharia_ready()),
        "sec_force_active": bool(SHARIA_SEC_FORCE_ACTIVE),
        "active_flag": str(SEC_SHARIA_ACTIVE_FLAG),
        "active_flag_exists": SEC_SHARIA_ACTIVE_FLAG.exists(),
        "active_flag_payload": _load_active_flag(),
        "companyfacts_zip": str(SEC_COMPANYFACTS_ZIP),
        "companyfacts_zip_exists": SEC_COMPANYFACTS_ZIP.exists(),
        "companyfacts_zip_size_mb": safe_round((SEC_COMPANYFACTS_ZIP.stat().st_size / 1024 / 1024), 1) if SEC_COMPANYFACTS_ZIP.exists() else 0,
        "tickers_exchange_json": str(SEC_TICKERS_EXCHANGE_JSON),
        "tickers_exchange_json_exists": SEC_TICKERS_EXCHANGE_JSON.exists(),
        "max_evidence_age_days": int(SEC_SHARIA_MAX_EVIDENCE_AGE_DAYS),
        "counts": {},
        "samples": {},
        "error": "",
    }
    try:
        init_sec_sharia_db()
        with _connect() as conn:
            out["counts"]["sec_company_map"] = int((conn.execute("SELECT COUNT(*) AS c FROM sec_company_map").fetchone() or {"c": 0})["c"])
            out["counts"]["sec_latest_financials"] = int((conn.execute("SELECT COUNT(*) AS c FROM sec_latest_financials").fetchone() or {"c": 0})["c"])
            rows = conn.execute("SELECT final_status, COUNT(*) AS c FROM sharia_screen_results GROUP BY final_status ORDER BY c DESC").fetchall()
            out["counts"]["by_final_status"] = {str(r["final_status"]): int(r["c"] or 0) for r in rows}
            for status in ["sec_clean", "sec_needs_review", "sec_blocked_financial", "sec_missing_data", "sec_stale_data"]:
                sample = conn.execute(
                    "SELECT symbol, final_status, label, reason, evidence_age_days, debt_ratio, cash_ratio, interest_ratio, filing_date, period_end FROM sharia_screen_results WHERE final_status=? ORDER BY symbol LIMIT ?",
                    (status, int(sample_limit or 12)),
                ).fetchall()
                out["samples"][status] = [dict(x) for x in sample]
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return out
