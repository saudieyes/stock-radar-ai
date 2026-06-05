# Clean Decision Core V1a — Full Integration

## الهدف
ربط عقد القرار النظيف بنتائج القوائم الرئيسية، وليس فقط روابط التشخيص الفردية.

## ما تغيّر
- تحديث `decision_contract.py` إلى `decision_contract_v1a_2026_06_05_full_integration`.
- تحديث `final_decision_engine.py` إلى `official_final_decision_engine_v2a_2026_06_05_full_contract_integration`.
- تعديل `main.py` حتى لا تفرض طبقات Early/Source القديمة No-Chase إذا لم يكن السهم ممتدًا صعودًا الآن.

## الإصلاحات العملية
- No-Chase لا يظهر بناءً على قمة تاريخية قديمة فقط؛ يجب أن يكون السعر ممتدًا صعودًا حاليًا.
- إذا السهم أصبح هابطًا/مكسورًا/تحت الوقف، تظهر حالة الخطة الحالية مثل `PLAN_BROKEN` أو `WAIT_REBOUND` أو `RECLAIM_REQUIRED` بدل No-Chase.
- إذا مصدر السعر مجهول أو نسبة التغير غير مؤكدة، يعتبر السعر غير مكتمل حتى لو ظهر رقم سعر.
- إذا الخطة غير مكتملة، يتم إخفاء أرقام الدخول/الهدف/الوقف بدل عرض 0.
- روابط التشخيص تعرض `hide_plan_numbers=true` عند عدم اكتمال الخطة.

## ما لم يتغير
- لا تعديل على SQLite.
- لا تعديل على Sharia.
- لا تعديل على Polygon Weekly Builder.
- لا تعديل على الواجهة.
- لا تعديل على الصيانة/الأرشفة.

## روابط الاختبار بعد الرفع
- `/health`
- `/diagnostics/decision-contract/symbol?symbol=ALM`
- `/diagnostics/decision-contract/symbol?symbol=RKLB`
- `/diagnostics/decision-contract/symbol?symbol=JOBY`
- `/diagnostics/decision-contract/symbol?symbol=FSTR`
- `/diagnostics/decision-contract/symbol?symbol=STRL`
- `/trade-scan`
- `/radar-live-refresh`
- `/telegram-alerts/status`

## قبول المرحلة
- ALM/JOBY: يجب أن يكونا `PLAN_BROKEN` إذا السعر تحت الوقف.
- RKLB: يجب أن يكون `DATA_INCOMPLETE`، وأرقام الخطة يجب ألا تظهر كـ 0.
- STRL: إذا صار هابطًا يجب أن يكون `WAIT_REBOUND` أو حالة خطة مناسبة وليس Buy/No-Chase.
- القوائم الرئيسية يجب أن تقل فيها حالات No-Chase السالبة أو المكسورة.
