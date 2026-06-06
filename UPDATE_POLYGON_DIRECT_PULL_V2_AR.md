# تحديث Polygon Direct Pull + Weekly Builder V2

## الهدف
إضافة سحب مباشر آمن من Massive/Polygon Flat Files، مع استمرار قاعدة عدم حفظ ملفات Polygon الخام في Railway أو GitHub أو SQLite.

## ما تم
- إضافة `app/polygon_flatfile_fetcher.py`:
  - يسحب ملفات `minute_aggs_v1` و `day_aggs_v1` إلى `/tmp` فقط.
  - يمنع السحب في الويكند وإجازات السوق الأمريكي.
  - يطبّق حد محاولات لكل تاريخ/نوع بيانات، افتراضيًا 3 محاولات.
  - يحفظ حالة صغيرة فقط في `polygon_flatfile_pull_state.json`.
  - يحذف الملفات الخام بعد التحليل.

- ترقية `app/polygon_weekly_builder.py` إلى V2:
  - قراءة `.csv` و `.csv.gz` مباشرة.
  - قراءة ZIP يحتوي ملفات `.csv.gz` مثل ملفات Massive/Polygon.
  - دعم مسارين منفصلين: minute و daily حتى لا تعتمد الأداة على التخمين من الاسم.
  - اختيار قائمة Weekly Priority مستقلة، وليست شراء مباشر.
  - حفظ compact JSON فقط عند `execute=true`.
  - فلترة معظم ETFs/الأدوات غير العادية عبر `data/companies.csv` حتى لا تختلط بقائمة الأسهم.
  - تحليل ملفات الدقيقة الكبيرة بطريقة أسرع: preselection من اليومي ثم قراءة الدقيقة للمرشحين فقط.

- إضافة endpoints:
  - `/polygon-weekly/flatfiles-status`
  - `/polygon-weekly/build-from-local?minute_path=...&daily_path=...&top_n=15&execute=false`
  - `/polygon-weekly/build-from-polygon?trade_date=YYYY-MM-DD&minute_days=3&daily_days=25&top_n=15&execute=false`

- تنظيفات مرتبطة:
  - RKLB/الحالات المشابهة: إذا السعر موجود لكن لا توجد خطة دخول/هدف/وقف، يظهر `NO_VALID_PLAN` / “لا توجد خطة قابلة للتنفيذ” بدل “بيانات غير مكتملة”.
  - منع تكرار عبارة “متأخر تقريبًا 15 دقيقة”.
  - إزالة/إنهاء صلاحية عبارات “إغلاق الجمعة قوي” بعد انتهاء اليوم التالي إذا لم يتم توليد قائمة جديدة تؤكدها.

## متغيرات Railway المطلوبة للسحب المباشر
المفتاح الحالي `POLYGON_API_KEY` مفيد للـ REST/fallback، لكن Flat Files حسب Massive تحتاج S3 Access Key و Secret Key.
أضف في Railway عند توفرها من لوحة Massive/Polygon:

- `POLYGON_FLATFILES_ENABLED=true`
- `POLYGON_FLATFILES_ACCESS_KEY=...`
- `POLYGON_FLATFILES_SECRET_KEY=...`
- اختياري: `POLYGON_FLATFILES_MAX_ATTEMPTS=3`
- اختياري: `POLYGON_WEEKLY_BUILDER_MAX_MINUTE_FILES=3`  
  يمكن رفعها يدويًا حتى 14 في إعادة بناء ويكند مقصودة، لكن الافتراضي 3 لحماية Railway.

## ملاحظات أمان
- لا تحفظ raw `.csv.gz`.
- لا ترفع raw files إلى GitHub.
- لا تحفظ raw files في SQLite.
- لا يتم السحب عند فتح الصفحة.
- Weekly Priority ليست BUY_NOW ولا ترسل Telegram.
- Telegram يبقى فقط عند `final_decision_code=BUY_NOW`.
