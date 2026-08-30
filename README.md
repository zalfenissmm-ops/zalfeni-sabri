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

A **single-file** bot that runs the ICT intraday strategy (H1 context → M15
liquidity/sweep → M5 MSS + Displacement + FVG → Retracement), pulls data from
**MetaTrader 5**, runs a **90-day backtest** on start, then runs **live every 5
minutes** and sends signals (Entry / SL / TP / RR) to **Telegram**.

Default symbol: **Gold (XAUUSD)**. Runs **24h** — there is no trading-session
filter.

### Strategy sequence

```
H1  : classify context (with-trend / reversal-correction) — label only, does NOT block
M15 : liquidity sweep (break a high/low then close back = trap)
M5  : MSS (by close) + Displacement (body >= 1.5 x ATR) + FVG
then: retracement into the FVG = entry
SL  : beyond the sweep extreme  |  TP : opposing liquidity  |  RR >= 1.5 or NO TRADE
```

### How to run

```bash
# 1) Set your Telegram token and chat id (top of the file, or as env vars)
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"

# 2) Full run: one 90-day backtest, then live every 5 minutes
python3 ict_smc_bot.py                    # defaults to XAUUSD

# other modes
python3 ict_smc_bot.py --backtest-only    # backtest only
python3 ict_smc_bot.py --live-only        # live only
python3 ict_smc_bot.py --selftest         # test the engine on synthetic data (no MT5)
```

### Requirements

- **Windows** + an open, logged-in **MetaTrader 5** terminal.
- `pip install MetaTrader5`
- Telegram: a bot from `@BotFather` + your chat id (steps are at the top of the file).

### Main tunables (command line)

| Option | Meaning | Default |
|---|---|---|
| `--symbol` | trading symbol | XAUUSD |
| `--rr-min` | minimum Risk:Reward | 1.5 |
| `--disp-atr-mult` | displacement candle size (x ATR) | 1.5 |
| `--disp-body-ratio` | body as fraction of range | 0.5 |
| `--sweep-lookback` | look for a sweep in the last N M15 candles | 24 |
| `--mss-window` | MSS must occur within N M5 candles after the sweep | 12 |
| `--atr-period` | ATR period | 14 |

### Backtest output (example)

```
======================================================================
  BACKTEST - XAUUSD - last 90 days
======================================================================
  Total signals   : 12
  Wins            : 7
  Losses          : 5
  Win rate        : 58.3%
  Net R           : +6.40 R
======================================================================
  ... details of every trade (entry/exit/SL/TP/RR/context) ...
```

### Live signal (Telegram) example

```
🟢 BUY  XAUUSD
Entry : 2345.60
SL    : 2342.10
TP    : 2358.90
RR    : 1 : 3.80
Context: H1 up (with-trend)
```

On NO TRADE it prints the reason (which stage stopped), e.g.
`NO TRADE - [BUY] Sweep found but no MSS on M5 within the window (12 candles).`

> Note: the backtest has no look-ahead (swings confirm with the correct lag).
> `--selftest` numbers are on **synthetic** data — for testing the code, not real performance.
