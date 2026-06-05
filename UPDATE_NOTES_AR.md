# Clean Decision Core V2 — Unified Visible Decision & Legacy Label Cleanup

## الهدف
هذا التحديث يكمل V1/V1a ويجعل القرار النهائي الحالي هو الحكم الظاهر في القوائم والتشخيصات، بدل أن تبقى تسميات legacy مثل No-Chase/الحركة متأخرة ظاهرة عندما يكون القرار الحقيقي هو انتظار ارتداد، استعادة، Pullback، خطة مكسورة، أو بيانات غير مكتملة.

## الملفات المعدلة
- app/final_decision_engine.py
- app/source_promotion_engine_v2.py
- app/source_promotion_v2a.py

## ما تم
1. إضافة مزامنة نهائية للحقول القديمة بعد final_decision_code.
2. إخفاء/تنظيف No-Chase من الحقول الظاهرة إذا final_decision_code ليس NO_CHASE.
3. حفظ حالة العرض الجديدة في:
   - visible_decision_code
   - visible_decision_label
   - visible_move_stage
   - visible_move_stage_label
   - visible_source_promotion_status
   - visible_source_promotion_list
4. تعديل ملخص Source Promotion V2 ليحسب stage/status/list من الحقول المرئية النهائية، لا من move_stage القديم.
5. تعديل ملخص Source Promotion V2a حتى لا يعرض No-Chase كسبب إذا القرار النهائي الحالي ليس NO_CHASE.
6. تنظيف early_movement من No-Chase القديم إذا القرار النهائي الحالي ليس NO_CHASE، مع إبقاء القرار النهائي واضحًا.

## اختبارات محلية
- python -m compileall على المشروع كاملًا.
- import main نجح.
- اختبار على trade-scan الأخير:
  - لا توجد No-Chase ظاهرة في الحقول المستخدمية لأي سهم final_decision_code لديه ليس NO_CHASE.
  - بقي No-Chase واحد فقط عندما كان final_decision_code = NO_CHASE.
- RKLB يبقى DATA_INCOMPLETE مع hide_plan_numbers.
- ALM/JOBY يبقيان PLAN_BROKEN.
- STRL يبقى WAIT_REBOUND عند الهبوط.
- FSTR يبقى WAIT_RESISTANCE عند مقاومة قريبة.

## ما لم يتم تغييره
- لا تغيير على SQLite.
- لا تغيير على Sharia.
- لا تغيير على Telegram logic نفسه بعد V1a، لكن Telegram يستفيد من final_decision_code النظيف.
- لا تغيير على Polygon Weekly Builder أو منطق المسح؛ هذه تأتي بعد تنظيف نواة القرار والعرض.
