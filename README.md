# Trend Identifier — أداة تحديد الترند

أداة بايثون (بدون مكتبات خارجية) لتحديد اتجاه السوق وقوته من بيانات الشموع (OHLC)،
باستخدام ثلاث إشارات متكاملة بدل الاعتماد على مؤشر واحد:

1. **بنية القمم والقيعان**: قمم/قيعان أعلى (صاعد) مقابل قمم/قيعان أدنى (هابط).
2. **موقع السعر من المتوسط المتحرك الأسي (EMA)**: فوقه = صاعد، تحته = هابط.
3. **ADX**: لقياس **قوة** الترند وتمييز الترند الحقيقي عن السوق العرضي.

## الاستخدام

ملف CSV يحتوي على الأعمدة: `date,open,high,low,close`

```bash
python3 trend_identifier.py data.csv --ema 50 --adx-period 14 --swing 5
```

### مثال على المخرجات

```
Direction : Uptrend
Strength  : strong (ADX=32.10)
Price vs EMA50: up (close=140.53000, ema=131.44000)
Swing structure: up
```

- **Direction**: Uptrend / Downtrend / Sideways / Unclear
- **Strength**: قوة الترند حسب ADX (weak/ranging < 20، developing 20-25، strong > 25)
- **Swing structure**: نمط القمم/القيعان الأخير (up / down / mixed / unknown إذا لم تتوفر بيانات كافية)

## ملاحظة

لا تعتمد على مؤشر واحد فقط. الإشارة الأقوى هي توافق الإشارات الثلاث معًا، والقوة (ADX)
تخبرك متى تثق بالاتجاه ومتى يكون السوق عرضيًا وغير موثوق للتداول باتجاه واحد.

---

## SMC Strategy — smc_strategy.py

أداة ثانية تكتشف إعدادات **Smart Money Concepts** من نفس نوع ملفات CSV:
سيولة (Equal Highs/Lows)، تصيّد السيولة (Liquidity Sweep)، هيكل السعر (BOS/CHoCH)،
أوردر بلوك (Order Block)، فجوة سعرية (FVG)، ومنطقة العلاوة/الخصم (Premium/Discount).

### الاستخدام

```bash
python3 smc_strategy.py data.csv --swing 1 --tolerance 0.0015 --impulse 1.8
```

- `--swing`: نافذة اكتشاف القمم/القيعان (كل ما قلّت، كل ما اكتشفت قمم/قيعان أقرب لبعضها).
- `--tolerance`: نسبة التفاوت المسموحة لاعتبار قمتين/قاعين "متساويين" = مجمع سيولة.
- `--impulse`: مضاعف حجم الشمعة عشان تعتبر "اندفاعية" لتحديد الأوردر بلوك.

### مثال على المخرجات

```
Direction        : SHORT (CHoCH at candle #13)
Liquidity sweep  : {'index': 11, 'kind': 'high', 'level': 81.0}
Order block      : {'index': 12, 'kind': 'bearish_ob', 'top': 80.0, 'bottom': 76.0}
FVG              : {'index': 13, 'kind': 'bearish', 'bottom': 65.0, 'top': 76.0}
Equilibrium (50%): 59.50000
In premium zone   : True
In order block   : True
In FVG           : False
Entry ready      : True
Suggested SL     : 81.08100
Suggested TP     : 37.96200
```

`Entry ready: True` يعني السعر رجع لمنطقة العلاوة (فوق 50% من الهبوط) وداخل الأوردر بلوك أو الفجوة السعرية —
نفس النقطة اللي كانت متوضحة في خريطة الاستراتيجية.

---

## SMC Order Block — مؤشر MQL5

**الملف:** `MQL5/Indicators/SMC_OrderBlock.mq5`

مؤشر لميتاتريدر 5 يكتشف **الأوردر بلوك الصحيح** ويرسمو على الشارت.
التعريف المعتمد: *آخر شمعة معاكسة قبل الـ displacement اللي كسّر الستركتشر*.

### الشروط الخمسة (لازم يتوفّرو مع بعض)

| # | الشرط | كيفاش يتحسب في الكود |
|---|-------|----------------------|
| 1 | **Liquidity sweep** | شمعة قبل الـ OB تنزل تحت (ولا تطلع فوق) قاع/قمة سابقة **وتسكّر راجعة داخل** — وإذا كان المستوى المكنوس عندو توأم في حدود `InpEqualTolerance` = **equal highs/lows** ونزيدو نقطة |
| 2 | **Displacement** | مدى الحركة ≥ `InpDispMult` × متوسط المدى، ونسبة الأجسام ≥ `InpMinBodyRatio` (موش فتايل)، وعدد الشموع ≤ `InpMaxDispBars` (موش تدريجية) |
| 3 | **BOS / MSS** | إغلاق وراء آخر قمة/قاع مؤكّد (fractal بنافذة `InpSwingWindow`). كسر ضد الاتجاه السابق = **MSS**، مع الاتجاه = **BOS** |
| 4 | **FVG** | imbalance ثلاثي داخل الحركة (`low[m+1] > high[m-1]` للصعود)، بحجم ≥ `InpMinFVGRatio` × متوسط المدى |
| 5 | **Unmitigated** | السعر ما رجعلهاش من بعد الكسر — طريقة الحساب في `InpMitigation` |

### كيفاش تتحدّد الزون

- نرجعو من شمعة الكسر لور لين أوّل شمعة معاكسة → هي الـ OB.
- شمعتين ولا ثلاثة معاكسين متتاليين → **كتلة وحدة** (`InpMaxOBCandles`، حطّها **1** إذا تحب شمعة وحيدة فقط).
- المدى: من الـ **open** للـ **high/low بالفتيل** (`OB_RANGE_OPEN_WICK` — الافتراضي).
  فمّا زادة `OB_RANGE_FULL` (الشمعة كاملة) و`OB_RANGE_BODY` (الجسم برك).
- **CE = 50%** متاع الزون → مرسوم بخط متقطّع، هو نقطة الدخول الأدق.

### Refinement

حطّ `InpDetectTF` على **H4/H1** و`InpRefineTF` على **M15/M5**:
المؤشر يلوّج داخل زمن الـ OB على التايم الأصغر ويرسملك **زون داخلية برتقالية** =
الـ FVG اللي داخل الـ OB (وإذا ما فماش، آخر شمعة معاكسة صغيرة).

### التقييم (Score / Grade)

كل زون تاخو نقاط: كسر ستركتشر +1، sweep +1، equal highs/lows +1، displacement +1،
FVG +1، unmitigated +1، premium/discount صحيح +1، تداخل مع **OTE 62–79%** +1.

- **A+** ≥ 7 &nbsp;•&nbsp; **A** ≥ 6 &nbsp;•&nbsp; **B** ≥ 5 &nbsp;•&nbsp; **C** = ضعيفة

الـ **C** هي بالضبط علامات الضعف اللي تعرفهم: بلا sweep، حركة بطيئة بلا imbalance،
مستهلكة (mitigated)، ولا في وسط الرينج موش في premium/discount الصحيح.
`InpMinStrength` يخلّيك تعرض برك اللي فوق مستوى معيّن.

### التنصيب

1. في ميتاتريدر: **File → Open Data Folder** → `MQL5/Indicators/`
2. حطّ فيها `SMC_OrderBlock.mq5`
3. في MetaEditor: **F7** باش تكومبايلي → المؤشر يظهر في Navigator

### أهمّ الإعدادات

| الإعداد | الافتراضي | الشرح |
|---------|-----------|-------|
| `InpDetectTF` | Current | تايم فريم الاكتشاف (H4/H1 مستحسن) |
| `InpSwingWindow` | 3 | نافذة القمم/القيعان — كل ما كبرت، الستركتشر يولّي أكبر |
| `InpMaxOBCandles` | 3 | أقصى شموع في الكتلة (1 = شمعة وحيدة) |
| `InpDispMult` | 1.8 | قوة الـ displacement |
| `InpMinBodyRatio` | 0.50 | باش ما تقبلش حركة فتايل |
| `InpMaxDispBars` | 5 | باش ما تقبلش حركة تدريجية |
| `InpRequireSweep/Disp/FVG` | true | الشروط الإجبارية |
| `InpRequireMSS` | false | حطّها true إذا تحب MSS برك (بلا BOS عادي) |
| `InpRequireDiscount` | false | true = ما تعرضش OB في وسط الرينج |
| `InpMitigation` | Touch edge | لمسة الحافة / CE / إغلاق داخل الزون |
| `InpDeleteBroken` | true | امسح الزون كي تنكسر (إغلاق وراها) |
| `InpRefine` / `InpRefineTF` | true / M15 | الـ refinement الداخلي |
| `InpAlertNewOB` / `InpAlertTap` | false | تنبيه عند OB جديدة / عند دخول السعر |
| `InpPrintLog` | false | يطبع في اللوج علاش تزادت ولا تلغات كل زون — مفيد برشا للمعايرة |

### قراية الرسم

- **مستطيل ملوّن** = الزون (أخضر صاعدة / أحمر هابطة، ورمادي باهت = mitigated بإطار منقّط).
- **خط متقطّع في الوسط** = CE 50%.
- **مستطيل أزرق منقّط** = الـ FVG متاع الحركة.
- **خط ذهبي `$` / `EQ$`** = مستوى السيولة اللي تكنس.
- **مستطيل برتقالي داخلي** = الزون المكرّرة (LTF refinement).
- **النص** = `BULL OB A+ | MSS SWEEP(EQ) DISP FVG FRESH DISC OTE`
