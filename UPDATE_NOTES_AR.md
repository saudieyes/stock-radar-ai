# Stock Radar AI — Clean Decision Core V1

## الملفات المعدلة
- main.py
- app/decision_contract.py
- app/final_decision_engine.py
- app/opportunity_intelligence.py
- app/pattern_action_engine.py
- app/single_stock_engine.py
- app/telegram_alerts.py

## ما تم تنفيذه
1. توحيد عقد السعر والخطة قبل القرار النهائي.
2. منع عرض خطة مكسورة كدخول بحذر أو تأكيد مبكر.
3. حصر لا تطارد في حالة امتداد صعودي فقط، وليس عند الهبوط أو كسر الدعم.
4. منع أرقام 0 أو بيانات ناقصة من الظهور كخطة قابلة للتنفيذ.
5. تحويل أنماط الرابحين والخاسرين إلى حقول عملية داخل البطاقة.
6. جعل Telegram لا يرسل إلا إذا BUY_NOW قابل للتنفيذ الآن.
7. إضافة رابط تشخيص قرار رمز واحد: /diagnostics/decision-contract/symbol?symbol=ALM

## اختبارات قبول محلية
- ALM: أصبحت الخطة مكسورة PLAN_BROKEN وليست دخول بحذر.
- RKLB: أصبحت بيانات غير مكتملة DATA_INCOMPLETE عند نقص التغير/الخطة.
- JOBY: أصبحت انتظار ارتداد WAIT_REBOUND وليست لا تطارد.
- FSTR: أصبحت انتظار استعادة RECLAIM_REQUIRED بسبب دعم مكسور وليست لا تطارد.
- STRL: لا يبقى BUY_NOW إلا إذا السعر داخل منطقة التنفيذ والسيولة والسعر موثوقة.

## ملاحظات
هذا التحديث لا يلمس SQLite أو Sharia أو الأرشفة أو ملفات Polygon. هو تحديث نواة القرار والتنبيه فقط.
