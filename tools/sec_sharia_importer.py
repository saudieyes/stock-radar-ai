#!/usr/bin/env python3
"""Import SEC companyfacts.zip into Stock Radar AI SQLite Sharia tables.

Usage examples:
  python tools/sec_sharia_importer.py --tickers /data/sec/company_tickers_exchange.json --facts /data/sec/companyfacts.zip
  python tools/sec_sharia_importer.py --symbols EHGO ICCM HOUR NIXX --facts /data/sec/companyfacts.zip
  python tools/sec_sharia_importer.py --limit 500
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Import SEC XBRL bulk facts into local Sharia financial screen tables.")
    parser.add_argument("--tickers", default=os.getenv("SEC_TICKERS_EXCHANGE_JSON", ""), help="Path to company_tickers_exchange.json")
    parser.add_argument("--facts", default=os.getenv("SEC_COMPANYFACTS_ZIP", ""), help="Path to companyfacts.zip")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional ticker subset to import first")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit for test imports")
    parser.add_argument("--skip-map", action="store_true", help="Do not import ticker/CIK map")
    args = parser.parse_args()

    # Import after sys.path/env setup.
    import json
    from app.sec_sharia_store import (
        SEC_COMPANYFACTS_ZIP,
        SEC_TICKERS_EXCHANGE_JSON,
        import_companyfacts_zip,
        import_sec_company_map,
        sec_sharia_status,
    )

    tickers = Path(args.tickers) if args.tickers else SEC_TICKERS_EXCHANGE_JSON
    facts = Path(args.facts) if args.facts else SEC_COMPANYFACTS_ZIP

    result = {"map": None, "facts": None, "status": None}
    if not args.skip_map:
        result["map"] = import_sec_company_map(tickers)
        print(json.dumps({"map": result["map"]}, ensure_ascii=False, indent=2), flush=True)
        if not result["map"].get("ok"):
            return 2

    result["facts"] = import_companyfacts_zip(facts, symbols=args.symbols, limit=args.limit or None)
    print(json.dumps({"facts": result["facts"]}, ensure_ascii=False, indent=2), flush=True)
    result["status"] = sec_sharia_status(sample_limit=5)
    print(json.dumps({"status": result["status"]}, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["facts"].get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
