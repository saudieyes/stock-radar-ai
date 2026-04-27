import csv
import os
from pathlib import Path

from .settings import DATA_DIR, BASE_DIR
from .utils import clean_key, clean_row, latest_key


def _resolve_path(path):
    p = Path(path)
    if p.exists():
        return str(p)
    alt = DATA_DIR / p.name
    if alt.exists():
        return str(alt)
    # Repository data fallback. In Railway DATA_DIR may point to /data or app_data,
    # while the bundled reference CSV files live under ./data in GitHub.
    repo_alt = BASE_DIR / "data" / p.name
    if repo_alt.exists():
        return str(repo_alt)
    return str(path)


def _first(row, names, default=""):
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
    # fallback normalized compare
    norm = {str(k).lower().replace(" ", "").replace("_", ""): k for k in row.keys()}
    for name in names:
        key = str(name).lower().replace(" ", "").replace("_", "")
        if key in norm and row.get(norm[key]) not in (None, ""):
            return row.get(norm[key])
    return default


def read_csv(path):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(f, dialect=dialect)
            rows = [clean_row(r) for r in reader]
            if rows and len(rows[0].keys()) > 1:
                return rows
        except Exception:
            pass

    for d in [";", ","]:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=d)
            rows = [clean_row(r) for r in reader]
            if rows and len(rows[0].keys()) > 1:
                return rows

    return []



def iter_csv_rows(path):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ";"
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            delimiter = dialect.delimiter
        except Exception:
            if "," in sample and sample.count(",") > sample.count(";"):
                delimiter = ","
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            yield clean_row(row)


def load_sector():
    data = {}
    for r in read_csv(DATA_DIR / "sector_industry.csv"):
        industry_id = str(_first(r, ["IndustryId", "Industry ID", "IndustryID", "industry_id", "Industry_Id"]) or "").strip()
        industry = str(_first(r, ["Industry", "industry", "IndustryName", "Industry Name"]) or "").strip()
        sector = str(_first(r, ["Sector", "sector", "SectorName", "Sector Name"]) or "").strip()
        if industry_id:
            data[industry_id] = {"industry": industry, "sector": sector}
    return data


def load_companies():
    data = {}
    for r in read_csv(DATA_DIR / "companies.csv"):
        t = str(_first(r, ["Ticker", "Symbol", "ticker", "symbol"]) or "").upper().strip()
        if t:
            data[t] = r
    return data


def load_latest(path):
    # Stream rows instead of materializing the whole CSV first. This keeps
    # startup fast even when financial statement files grow.
    data = {}
    for r in iter_csv_rows(path) or []:
        t = str(_first(r, ["Ticker", "Symbol", "ticker", "symbol"]) or "").upper().strip()
        if not t:
            continue
        k = latest_key(r)
        if t not in data or k > data[t]["_k"]:
            r["_k"] = k
            data[t] = r
    for t in data:
        data[t].pop("_k", None)
    return data


SECTOR_DATA = load_sector()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest(DATA_DIR / "balance_sheet.csv")
INCOME_DATA = load_latest(DATA_DIR / "income_statement.csv")


def initialize_reference_data():
    sector_data = load_sector()
    companies_data = load_companies()
    balance_data = load_latest(DATA_DIR / "balance_sheet.csv")
    income_data = load_latest(DATA_DIR / "income_statement.csv")
    return sector_data, companies_data, balance_data, income_data

