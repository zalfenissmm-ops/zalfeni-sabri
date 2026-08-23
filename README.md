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

## Trend Following Strategy — trend_following_strategy.py

أداة ثالثة تعطي إشارة **LONG / SHORT / FLAT** جاهزة للتداول باتجاه الترند، بدل الاكتفاء بوصف الاتجاه فقط.
تجمع بين:

1. **تقاطع EMA**: EMA سريع فوق EMA بطيء = صاعد، تحته = هابط.
2. **بنية القمم والقيعان**: ما تدخلش LONG إذا البنية هابطة، ولا SHORT إذا البنية صاعدة.
3. **ADX**: فلتر قوة — ما فماش إشارة إلا إذا الترند قوي (ADX ≥ حد أدنى).
4. **Trailing Stop بـ ATR** (Chandelier Exit): أعلى قمة (أو أدنى قاع) في آخر نافذة زمنية مطروح منها/مضاف ليها مضاعف × ATR،
   بدل هدف ربح ثابت — الفكرة تخلي الصفقة تركب الترند وما تقصوش بسرعة.

### الاستخدام

```bash
python3 trend_following_strategy.py data.csv --fast 20 --slow 50 --adx-threshold 25 --atr-mult 3
```

### مثال على المخرجات

```
Signal              : LONG
EMA20/EMA50        : 173.27081 / 163.37188
Structure           : up
ADX(14)            : 32.10 (strong)
ATR(14)            : 1.62752
Last close          : 179.46000
Trailing stop       : 174.66743
```

- `Signal`: LONG (ادخل شراء) / SHORT (ادخل بيع) / FLAT (ابقى برا السوق).
- `Trailing stop`: نقطة الخروج المتحركة — تتحدث في كل شمعة جديدة، ما فماش هدف ربح ثابت.
