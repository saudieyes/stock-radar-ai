import csv
import os

from .settings import DATA_DIR
from .utils import clean_key, clean_row, latest_key

def read_csv(path):
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(f, dialect=dialect)
            rows = [clean_row(r) for r in reader]
            if rows:
                return rows
        except:
            pass

    for d in [";", ","]:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=d)
            rows = [clean_row(r) for r in reader]
            if rows and len(rows[0].keys()) > 1:
                return rows

    return []


def load_sector():
    data = {}
    for r in read_csv("data/sector_industry.csv"):
        industry_id = str(r.get("IndustryId", "")).strip()
        if industry_id:
            data[industry_id] = {
                "industry": str(r.get("Industry", "")).strip(),
                "sector": str(r.get("Sector", "")).strip()
            }
    return data


def load_companies():
    data = {}
    for r in read_csv("data/companies.csv"):
        t = str(r.get("Ticker", "")).upper().strip()
        if t:
            data[t] = r
    return data


def load_latest(path):
    data = {}
    for r in read_csv(path):
        t = str(r.get("Ticker", "")).upper().strip()
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
BALANCE_DATA = load_latest("data/balance_sheet.csv")
INCOME_DATA = load_latest("data/income_statement.csv")

def initialize_reference_data():
    sector_data = load_sector()
    companies_data = load_companies()
    balance_data = load_latest(str(DATA_DIR / "balance_sheet.csv"))
    income_data = load_latest(str(DATA_DIR / "income_statement.csv"))
    return sector_data, companies_data, balance_data, income_data
