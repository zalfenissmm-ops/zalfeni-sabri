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

## 🔥 ICT/SMC Intraday Bot — `ict_smc_bot.py`

بوت **ملف واحد** يطبّق استراتيجية ICT الانترادي (H1 سياق → M15 سيولة/Sweep →
M5 MSS + Displacement + FVG → Retracement)، يجيب البيانات من **MetaTrader 5**،
يعمل **Backtest 90 يوم** عند التشغيل، ثم يشتغل **مباشر كل 5 دقايق** و يبعث
إشارات (Entry / SL / TP / RR) على **تيليقرام**.

### تسلسل الاستراتيجية

```
H1  : تصنيف السياق (مع الاتجاه / انعكاس-تصحيح) — تصنيف برك، ما يمنعش الصفقة
M15 : Liquidity Sweep (كسر قمة/قاع + رجوع = فخ)
M5  : MSS (بالإغلاق) + Displacement (جسم ≥ 1.5×ATR) + FVG
ثم  : Retracement للـ FVG = الدخول
SL  : تحت/فوق نقطة الـ Sweep   |   TP : السيولة المقابلة   |   RR ≥ 1.5 و إلا NO TRADE
```

### طريقة التشغيل

```bash
# 1) حط التوكن و الـ Chat ID (في أعلى الملف أو كمتغيرات بيئة)
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"

# 2) التشغيل الكامل: Backtest 90 يوم مرة وحدة، ثم مباشر كل 5 دقايق
python3 ict_smc_bot.py --symbol EURUSD

# خيارات أخرى
python3 ict_smc_bot.py --backtest-only          # backtest برك
python3 ict_smc_bot.py --live-only              # مباشر برك
python3 ict_smc_bot.py --selftest               # تجربة المحرّك ببيانات وهمية (بلا MT5)
```

### المتطلبات

- **Windows** + تيرمينال **MetaTrader 5** مفتوح و مسجّل دخول.
- `pip install MetaTrader5`
- تيليقرام: بوت من `@BotFather` + الـ Chat ID (الطريقة مشروحة أعلى الملف).

### أهم الأرقام القابلة للتعديل (من سطر الأوامر)

| الخيار | المعنى | الافتراضي |
|---|---|---|
| `--rr-min` | أقل Risk:Reward مقبول | 1.5 |
| `--disp-atr-mult` | حجم شمعة الاندفاع (× ATR) | 1.5 |
| `--disp-body-ratio` | نسبة الجسم من المدى | 0.5 |
| `--sweep-lookback` | نبحث على Sweep في آخر كم شمعة M15 | 24 |
| `--mss-window` | الـ MSS لازم يصير خلال كم شمعة M5 بعد الـ Sweep | 12 |
| `--atr-period` | فترة ATR | 14 |

### مثال مخرجات الباكتيست

```
======================================================================
  BACKTEST — EURUSD — آخر 90 يوم
======================================================================
  إجمالي الإشارات      : 12
  رابحة (WIN)          : 7
  خاسرة (LOSS)         : 5
  نسبة الربح (Win rate): 58.3%
  صافي R               : +6.40 R
======================================================================
  ... تفاصيل كل صفقة (دخول/خروج/SL/TP/RR/سياق) ...
```

### مثال إشارة مباشرة (تيليقرام)

```
🟢 BUY  EURUSD
Entry : 1.08540
SL    : 1.08390
TP    : 1.09150
RR    : 1 : 4.10
Context: H1 صاعد (مع الاتجاه)
```

و كان NO TRADE يطبع علاش (أي مرحلة وقفت): مثلاً
`NO TRADE — [BUY] فما Sweep أما ما صارش MSS على M5 داخل النافذة (12 شمعة).`

> ملاحظة: الباكتيست بلا نظر للمستقبل (القمم/القيعان تتأكد بالتأخير الصحيح).
> نتائج `--selftest` على بيانات **وهمية** للتجربة برك، موش أداء حقيقي.
