# V2W9e Official Update

- Added FMP-only final after-hours sweep after the extended session ends.
- Added source session date, intended trading date, started/completed timestamps in ET/KSA.
- Mark regular close as reference-only when no extended price is returned during premarket/after-hours.
- Faster page opening: render saved scan first, hydrate live/extended prices in the background.


## V2W9f — Tomorrow Prep Final Bridge to Live Lists

- Bridges final V2W9e after-hours Tomorrow Prep rows into live visible Pre-Trigger and Low-Float sections.
- Prioritizes these symbols in live quote refresh to prevent stale snapshot-only lists.
- Adds monitoring-only trigger/stop/target fields for bridged prep rows.
- Adds `/diagnostics/list-freshness`.
