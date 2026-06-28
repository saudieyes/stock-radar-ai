# V2W18 — iPhone Card Responsive Fix + Sharia Filter Audit

Version goal: fix iPhone/Safari card layout readability and add a Sharia audit endpoint without changing original radar decisions.

## UI
- Adds final mobile CSS guard for compact-modern cards.
- Prevents ticker symbols from truncating/ellipsis on narrow iPhone widths.
- Moves compact price presentation into its own readable row on narrow screens.
- Avoids displaying `0` as current price when no positive price candidate exists; shows `—` instead.
- Adds safer display price fallback order.

## Backend diagnostics
New endpoint:

`GET /diagnostics/sharia-filter-audit`

It reads the latest saved scan/live refresh snapshots and reports:
- raw snapshot Sharia composition,
- visible actionable section Sharia composition,
- synthetic strong/cautious/gray/blocked groups from the saved snapshot,
- whether the filter looks relaxed/bypassed,
- whether the issue is likely new-small-stock unknown pressure.

## Safety
Audit only. No changes to ranking, BUY_NOW, Telegram, original tool analysis, or Sharia decisions.
