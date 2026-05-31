# Stock Radar AI Official Update Notes

This update introduces a single final decision layer, optional Telegram alerts, weekly Polygon priority watchlist data, cost/retention diagnostics, UI scroll fixes, and safer memory/reference-data handling.

Important deploy cleanup:
- Remove `app_data/evidence_archive/` from the Railway-deployed GitHub code tree after confirming archives are already synced/readable in GitHub.
- Keep runtime/generated data on Railway `/data` or GitHub archive, not inside application code.
- Telegram stays disabled unless both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are added in Railway.

New endpoints:
- `/system-cost-health`
- `/diagnostics/system-cost-health`
- `/telegram-alerts/status`

Final Strong Entry rule:
- `دخول قوي` is only displayed when `final_decision_code=BUY_NOW` after live liquidity, entry proximity, resistance, prior movement, risk/reward, and price reliability checks.


Fix4 scroll stability: preserve radar scroll after Sharia exclude/approve/restore and after full scan completion.


## Fix5 final scroll stability
- Manual Sharia approve/exclude/restore no longer rebuilds or removes radar cards immediately. It marks the card locally and keeps layout height stable, preventing bottom-page jumps.
- Full scan no longer collapses existing radar content into a loading box when data already exists.
- Automatic periodic full refresh is skipped when the user is deep in the page to avoid unexpected scroll jumps while reading or using Sharia lists.
