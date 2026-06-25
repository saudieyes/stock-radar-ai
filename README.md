# Stock Radar AI — V2W9e

V2W9e adds an FMP-only final after-hours sweep after 20:05 ET / about 03:05 KSA, clear completion timestamps in Tomorrow Prep status, safer extended-price labels, and a faster initial UI render.

Key checks:

- `/tomorrow-prep/status?format=brief`
- `/tomorrow-prep/after-hours-final-sweep?execute=true&max_batches=6&format=brief` only if needed.

The final sweep does not depend on Polygon. Polygon remains optional confirmation only.


## V2W9f — Tomorrow Prep Final Bridge to Live Lists

ربط مباشر بين قائمة V2W9e النهائية بعد after-hours وقائمتي Pre-Trigger و Low-Float في /trade-scan و /radar-live-refresh. يضيف /diagnostics/list-freshness للتحقق من مصدر وحداثة القائمتين.
