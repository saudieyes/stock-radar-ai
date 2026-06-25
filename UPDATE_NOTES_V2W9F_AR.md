# V2W9f — Tomorrow Prep Final Bridge to Live Lists

هذا تحديث صغير فوق V2W9e، هدفه ربط قائمة الغد النهائية بعد فحص after-hours بقوائم الواجهة التي بدت ثابتة.

## ما تغير

- إضافة جسر `tomorrow_prep_final_bridge_to_live_lists_v2w9f_2026_06_25`.
- عند فتح `/radar-live-refresh` يتم حقن مرشحي Tomorrow Prep النهائيين داخل صفوف الرادار قبل تحديث الأسعار.
- عند فتح `/trade-scan` من الكاش أو من فحص جديد يتم حقن نفس الجسر داخل الاستجابة.
- قائمة `Low-Float / Pre-Market Radar` تحصل الآن على مرشحي `low_float_proxy` من فحص V2W9e النهائي.
- قائمة `قريب من التفعيل / Pre-Trigger` تحصل الآن على مرشحي `pre_trigger` من فحص V2W9e النهائي.
- رموز الجسر توضع في بداية قائمة الرموز المطلوبة من FMP حتى يأخذها تحديث السعر أولًا.
- كل بطاقة من الجسر تحمل خطة مراقبة: trigger / invalidation / target، ولا تتحول إلى BUY أو Strong/Cautious.
- إضافة تشخيص جديد:
  - `/diagnostics/list-freshness`

## ما لم يتغير

- لا تغيير في Strong/Cautious أو Telegram.
- لا اعتماد على Polygon.
- لا يتم إظهار المحظور شرعيًا من القائمة اليدوية.
- الجسر مراقبة فقط، لا شراء مباشر.

## كيف تتحقق بعد الرفع

افتح:

```text
/radar-live-refresh?limit=25&prefer_cache=false
```

وابحث عن:

```text
tomorrow_prep_final_bridge_v2w9f.used = true
```

ثم افتح:

```text
/diagnostics/list-freshness
```

المطلوب:

```text
pre_trigger.fresh = true
low_float_premarket.fresh = true
```
