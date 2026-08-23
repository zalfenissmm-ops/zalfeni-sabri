## البداية السريعة — trend_bot.py (ملف واحد، أمر واحد)

كل شيء (إشارة LONG/SHORT/FLAT + باك-تست + جلب مباشر من MT5) مجمّع في ملف واحد `trend_bot.py`،
يخدم بأمر واحد بلا ما تحتاج تركّب بقية الملفات:

```bash
# من ملف CSV محلي
python3 trend_bot.py data.csv --verbose

# مباشرة من MT5 (يحتاج ترمينال MT5 محلول + pip install MetaTrader5)
python3 trend_bot.py --mt5 --symbol EURUSD --timeframe H1 --count 1000 --out eurusd_h1.csv
```

يطبعلك الإشارة الحالية ثم نتائج الباك-تست فنفس الوقت. بقية الملفات فالريبو (`trend_identifier.py`,
`smc_strategy.py`, `trend_following_strategy.py`, `trend_following_backtest.py`, `mt5_fetch.py`)
باقيين موجودين لمن يحب يخدم بيهم منفصلين أو يبني عليهم.

### وضع المراقبة الحية — `--live`

تشغّل الأمر **مرة وحدة**، يطلعلك باك-تست آخر N يوم (`--days`)، وبعدها يدخل فمراقبة مستمرة: يعاود
يحلل البيانات كل `--interval` دقايق (افتراضي 5)، وما يطبعش حاجة إلا إذا طلعت **صفقة جديدة فعلية**
(الإشارة تبدلت لـ LONG أو SHORT) — فتلقى `Entry` و `Take Profit` و `Stop Loss`. إذا ماكانش تغيير، يطبع
"No new trade" ويكمل.

```bash
python3 trend_bot.py --mt5 --symbol EURUSD --timeframe M15 --live --days 30 --interval 5
```

- `--days 30`: الباك-تست المعروض عند البداية يقتصر على آخر 30 يوم فقط.
- `--live`: يفعّل المراقبة المستمرة بعد الباك-تست (Ctrl+C باش توقفها).
- `--interval`: كل قداش دقيقة يعاود يجيب بيانات جديدة ويحلل (افتراضي 5).
- `--rr`: نسبة المخاطرة/الربح لحساب Take Profit (افتراضي 2 — يعني TP = 2× مسافة الـ Stop Loss).

`--live` يخدم زادة مع ملف CSV عادي (بلا `--mt5`) — فهاذيك الحالة يعاود يقرا نفس الملف كل دورة،
فلازم حاجة أخرى (مثلاً EA أو سكريبت خارجي) تحدّث الملف باستمرار باش يبان تغيير حقيقي. مع `--mt5`
البيانات تتجدد بروحها كل دورة لأنها تجي مباشرة من الترمينال.

---

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

---

## Backtest — trend_following_backtest.py

يجرب استراتيجية `trend_following_strategy.py` على تاريخ كامل من البيانات (walk-forward، شمعة بشمعة)
بلا أي "لوك-أهيد" — فكل شمعة يستعمل غير البيانات اللي كانت موجودة لحد تلك اللحظة (نفس دالة الإشارة
المستعملة فالأداة الحية، باش الباك-تست والإشارة الحية ما يختلفوش).

### الاستخدام

```bash
python3 trend_following_backtest.py data.csv --capital 10000 --fee 0.05 --verbose
```

- `--capital`: رأس المال البداية (افتراضي 10000).
- `--fee`: تكلفة الدخول+الخروج كنسبة من قيمة الصفقة (افتراضي 0).
- `--verbose`: يطبع كل صفقة (تاريخ الدخول/الخروج، السعر، عدد الشموع، سبب الخروج، %PnL) قبل الملخص.
- بقية الخيارات (`--fast --slow --adx-period --adx-threshold --atr-period --atr-mult --swing`) نفس خيارات `trend_following_strategy.py`.

### مثال على المخرجات

```
Trades           : 9 (9 win / 0 loss)
Win rate         : 100.00%
Total return     : +157.44% (equity 10000.00 -> 25744.40)
Avg win / loss   : +11.29% / +0.00%
Profit factor    : inf
Max drawdown     : 0.00%
```

## ملاحظة على الباك-تستينق

الدخول/الخروج يتحسبو بسعر الإغلاق (close) لشمعة الإشارة — تبسيط معقول، لكن فالواقع لازم تستنى الشمعة تكمل
باش تعرف الإشارة، يعني التنفيذ الحقيقي يتأخر شمعة وحدة. الوقف المتحرك (trailing stop) يتفحص بالـ high/low
لكل شمعة، وإذا تلمس، الخروج يكون بسعر الوقف نفسه.

---

## جلب البيانات من MT5 — mt5_fetch.py

يسحب الشموع مباشرة من ترمينال MetaTrader 5 ويكتبها CSV بنفس صيغة `date,open,high,low,close`
اللي تقراها كل الأدوات الفوق.

### الشروط

- ترمينال **MT5 مثبت ومفتوح على نفس الجهاز** اللي تخدم فيه السكريبت (Windows، أو Wine) — مكتبة
  `MetaTrader5` تتكلم مع الترمينال المحلي وما تتصلش بأي API عن بعد، فما تنجمش تخدم من بيئة سحابية بلا MT5.
- `pip install MetaTrader5`

### الاستخدام

```bash
# آخر 1000 شمعة
python3 mt5_fetch.py --symbol EURUSD --timeframe H1 --count 1000 --out eurusd_h1.csv

# مدى تاريخي محدد
python3 mt5_fetch.py --symbol EURUSD --timeframe M15 --from 2024-01-01 --to 2024-06-01 --out data.csv

# تسجيل دخول لحساب معيّن بدل استعمال الجلسة المفتوحة في الترمينال
python3 mt5_fetch.py --symbol XAUUSD --timeframe H4 --count 2000 --out xauusd_h4.csv \
    --login 12345678 --password "..." --server "Broker-Server"
```

بعد ما يطلعلك ملف CSV، تنجم تخدم بيه مباشرة مع بقية الأدوات:

```bash
python3 mt5_fetch.py --symbol EURUSD --timeframe H1 --count 1000 --out eurusd_h1.csv
python3 trend_following_strategy.py eurusd_h1.csv
python3 trend_following_backtest.py eurusd_h1.csv --verbose
```

**Timeframes المتاحة**: `M1 M2 M3 M4 M5 M6 M10 M12 M15 M20 M30 H1 H2 H3 H4 H6 H8 H12 D1 W1 MN1`
