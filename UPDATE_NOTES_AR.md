# Stock Radar AI — Big Clean Source & Monitoring Update V1

هذا التحديث يبدأ المرحلة الأكبر بعد تثبيت Clean Decision Core V2.

## ما تم

1. **Quote Resolver V1**
   - قاعدة واحدة للسعر: FMP أولًا، ثم Polygon fallback عند فشل/نقص FMP.
   - إذا استخدم Polygon، يظهر أنه متأخر تقريبًا 15 دقيقة ومراقبة فقط، وليس تنفيذًا مباشرًا.
   - لا يسمح السعر المتأخر بترقية BUY_NOW أو دخول قوي.

2. **ربط فحص السهم الواحد بالسعر الموحد**
   - `/single-stock` و`/diagnostics/decision-contract/symbol` يحاولان الآن FMP ثم Polygon قبل إعلان نقص البيانات.
   - يمنع بقاء RKLB مثلًا على مصدر unknown إذا Polygon أعطى بيانات صالحة.

3. **Early Watch Lifecycle V1**
   - المراقبة المبكرة أصبحت لها حالة متابعة واضحة:
     - مراقبة لصيقة
     - قريب من التفعيل
     - يحتاج Pullback
     - يحتاج Reclaim
     - الخطة مكسورة
     - بيانات ناقصة
     - لا تطارد
   - الأداة تتابع السهم؛ المراقبة ليست أمر شراء للمستخدم.

4. **مسح أسرع لكن آمن**
   - تم تقليل فواصل المسح العميق بشكل محسوب:
     - أول ساعة: 7 دقائق تقريبًا بدل 10
     - وسط الجلسة: 12 دقيقة بدل 25
     - آخر ساعة: 8 دقائق تقريبًا
     - قبل/بعد السوق: أسرع لكن بدون مسح كل دقيقة
   - السعر الحي لا يزال يحدث أسرع من المسح العميق.

5. **Polygon Weekly Builder V1**
   - أضيفت بنية تحليل ملفات Polygon اليومية/الدقيقة من مسار محلي مؤقت.
   - تحفظ فقط الناتج المختصر، لا ملفات الدقيقة الخام.
   - تدخل نتائجها لاحقًا إلى المنبع كـ `polygon_weekly_builder` مستقل عن Auto-Detected Early Movement.

6. **روابط تشخيص جديدة**
   - `/diagnostics/quote-resolver/symbol?symbol=RKLB`
   - `/diagnostics/scan-cadence`
   - `/polygon-weekly/status`
   - `/polygon-weekly/build-from-local?path=app_data/polygon_weekly_input.zip&top_n=15&execute=false`

## ما لم يتم اعتباره شراء مباشر

- Polygon fallback = مراقبة فقط.
- Early Watch = الأداة تتابع، وليس المستخدم يشتري.
- Polygon Weekly list = قائمة أولوية للأسبوع، وليست BUY_NOW.
- Telegram لا يزال BUY_NOW فقط.

## اختبارات محلية

- compileall نجح.
- import main نجح.
- اختبار Polygon delayed يمنع BUY_NOW.
- اختبار Early Watch Lifecycle يعمل.
- اختبار Polygon Weekly Builder على CSV تجريبي نجح.
