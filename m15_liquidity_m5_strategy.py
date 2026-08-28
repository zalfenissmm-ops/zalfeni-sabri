import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta, timezone


# ============================================================
# SETTINGS
# ============================================================

SYMBOL = "EURUSD"
DAYS = 30

SWING_LOOKBACK = 3
RISK_REWARD = 2.0

# عدد شموع M5 المسموح بها بعد الوصول لمنطقة السيولة
MAX_BOS_CANDLES = 20

# عدد شموع M5 للبحث عن Pullback
MAX_PULLBACK_CANDLES = 10


# ============================================================
# MT5 CONNECTION
# ============================================================

if not mt5.initialize():
    print("ERROR: MT5 initialization failed")
    quit()

print("MT5 Connected")


# ============================================================
# GET DATA
# ============================================================

def get_data(symbol, timeframe, days):

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    rates = mt5.copy_rates_range(
        symbol,
        timeframe,
        start,
        end
    )

    if rates is None or len(rates) == 0:
        print("ERROR: No data")
        return None

    df = pd.DataFrame(rates)

    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True
    )

    return df


df_m15 = get_data(
    SYMBOL,
    mt5.TIMEFRAME_M15,
    DAYS
)

df_m5 = get_data(
    SYMBOL,
    mt5.TIMEFRAME_M5,
    DAYS
)

if df_m15 is None or df_m5 is None:
    mt5.shutdown()
    quit()

print("M15 candles:", len(df_m15))
print("M5 candles:", len(df_m5))


# ============================================================
# DETECT SWINGS
# ============================================================

def detect_swings(df, lookback=3):

    df = df.copy()

    df["swing_high"] = False
    df["swing_low"] = False

    for i in range(
        lookback,
        len(df) - lookback
    ):

        current_high = df.loc[i, "high"]
        current_low = df.loc[i, "low"]

        left_highs = df.loc[
            i - lookback:i - 1,
            "high"
        ]

        right_highs = df.loc[
            i + 1:i + lookback,
            "high"
        ]

        left_lows = df.loc[
            i - lookback:i - 1,
            "low"
        ]

        right_lows = df.loc[
            i + 1:i + lookback,
            "low"
        ]

        # SWING HIGH
        if (
            current_high > left_highs.max()
            and current_high > right_highs.max()
        ):
            df.loc[i, "swing_high"] = True

        # SWING LOW
        if (
            current_low < left_lows.min()
            and current_low < right_lows.min()
        ):
            df.loc[i, "swing_low"] = True

    return df


df_m15 = detect_swings(
    df_m15,
    SWING_LOOKBACK
)

df_m5 = detect_swings(
    df_m5,
    SWING_LOOKBACK
)


# ============================================================
# M15 MARKET STRUCTURE
# HH + HL = BUY
# LH + LL = SELL
# ============================================================

def detect_m15_trend(df):

    df = df.copy()

    df["trend"] = "NO_TRADE"

    swing_highs = []
    swing_lows = []

    for i in range(len(df)):

        if df.loc[i, "swing_high"]:

            swing_highs.append({
                "price": df.loc[i, "high"],
                "time": df.loc[i, "time"]
            })

        if df.loc[i, "swing_low"]:

            swing_lows.append({
                "price": df.loc[i, "low"],
                "time": df.loc[i, "time"]
            })

        # نحتاج على الأقل آخر قمتين وآخر قاعين
        if (
            len(swing_highs) >= 2
            and len(swing_lows) >= 2
        ):

            last_high = swing_highs[-1]["price"]
            previous_high = swing_highs[-2]["price"]

            last_low = swing_lows[-1]["price"]
            previous_low = swing_lows[-2]["price"]

            # HH + HL
            if (
                last_high > previous_high
                and last_low > previous_low
            ):
                df.loc[i, "trend"] = "BUY"

            # LH + LL
            elif (
                last_high < previous_high
                and last_low < previous_low
            ):
                df.loc[i, "trend"] = "SELL"

            else:
                df.loc[i, "trend"] = "NO_TRADE"

    return df


df_m15 = detect_m15_trend(df_m15)


# ============================================================
# M15 LIQUIDITY
#
# BUY TREND:
#   Main liquidity = previous Swing Low
#
# SELL TREND:
#   Main liquidity = previous Swing High
# ============================================================

def detect_m15_liquidity(df):

    df = df.copy()

    df["liquidity_type"] = None
    df["liquidity_level"] = None

    swing_highs = []
    swing_lows = []

    for i in range(len(df)):

        if df.loc[i, "swing_high"]:

            swing_highs.append(
                df.loc[i, "high"]
            )

        if df.loc[i, "swing_low"]:

            swing_lows.append(
                df.loc[i, "low"]
            )

        trend = df.loc[i, "trend"]

        # ==============================================
        # BUY
        # السيولة المهمة تحت آخر Swing Low
        # ==============================================

        if trend == "BUY":

            if len(swing_lows) > 0:

                liquidity_level = swing_lows[-1]

                df.loc[
                    i,
                    "liquidity_type"
                ] = "SELL_SIDE_LIQUIDITY"

                df.loc[
                    i,
                    "liquidity_level"
                ] = liquidity_level

        # ==============================================
        # SELL
        # السيولة المهمة فوق آخر Swing High
        # ==============================================

        elif trend == "SELL":

            if len(swing_highs) > 0:

                liquidity_level = swing_highs[-1]

                df.loc[
                    i,
                    "liquidity_type"
                ] = "BUY_SIDE_LIQUIDITY"

                df.loc[
                    i,
                    "liquidity_level"
                ] = liquidity_level

    return df


df_m15 = detect_m15_liquidity(df_m15)


# ============================================================
# TRANSFER M15 DATA TO M5
#
# M5 يحصل على:
# - Trend
# - Liquidity Type
# - Liquidity Level
# ============================================================

m15_context = df_m15[
    [
        "time",
        "trend",
        "liquidity_type",
        "liquidity_level"
    ]
].copy()


df_m5 = pd.merge_asof(

    df_m5.sort_values("time"),

    m15_context.sort_values("time"),

    on="time",

    direction="backward"

)


# ============================================================
# DETECT LIQUIDITY SWEEP ON M5
# ============================================================

def detect_buy_sweep(
    df,
    i,
    liquidity_level
):

    if pd.isna(liquidity_level):
        return False

    low = df.loc[i, "low"]
    close = df.loc[i, "close"]

    # السعر يكسر مستوى السيولة للأسفل
    # ثم يغلق فوقه

    if (
        low < liquidity_level
        and close > liquidity_level
    ):
        return True

    return False



def detect_sell_sweep(
    df,
    i,
    liquidity_level
):

    if pd.isna(liquidity_level):
        return False

    high = df.loc[i, "high"]
    close = df.loc[i, "close"]

    # السعر يكسر مستوى السيولة للأعلى
    # ثم يغلق تحته

    if (
        high > liquidity_level
        and close < liquidity_level
    ):
        return True

    return False


# ============================================================
# DETECT BOS AFTER SWEEP
# ============================================================

def detect_buy_bos(df, sweep_index):

    # آخر Swing High قبل الـ Sweep
    previous_highs = []

    start = max(
        0,
        sweep_index - 30
    )

    for i in range(
        start,
        sweep_index
    ):

        if df.loc[i, "swing_high"]:

            previous_highs.append(
                df.loc[i, "high"]
            )

    if len(previous_highs) == 0:
        return None

    structure_high = previous_highs[-1]

    end = min(
        sweep_index + MAX_BOS_CANDLES,
        len(df)
    )

    # BOS = Close فوق آخر Swing High

    for i in range(
        sweep_index + 1,
        end
    ):

        if df.loc[i, "close"] > structure_high:

            return {
                "index": i,
                "level": structure_high
            }

    return None



def detect_sell_bos(df, sweep_index):

    # آخر Swing Low قبل الـ Sweep
    previous_lows = []

    start = max(
        0,
        sweep_index - 30
    )

    for i in range(
        start,
        sweep_index
    ):

        if df.loc[i, "swing_low"]:

            previous_lows.append(
                df.loc[i, "low"]
            )

    if len(previous_lows) == 0:
        return None

    structure_low = previous_lows[-1]

    end = min(
        sweep_index + MAX_BOS_CANDLES,
        len(df)
    )

    # BOS = Close تحت آخر Swing Low

    for i in range(
        sweep_index + 1,
        end
    ):

        if df.loc[i, "close"] < structure_low:

            return {
                "index": i,
                "level": structure_low
            }

    return None


# ============================================================
# PULLBACK AFTER BOS
# ============================================================

def find_buy_pullback(
    df,
    bos_index,
    bos_level
):

    end = min(
        bos_index + MAX_PULLBACK_CANDLES,
        len(df)
    )

    for i in range(
        bos_index + 1,
        end
    ):

        # السعر يرجع لمنطقة BOS

        if (
            df.loc[i, "low"] <= bos_level
            and df.loc[i, "close"] > bos_level
        ):

            return i

    return None



def find_sell_pullback(
    df,
    bos_index,
    bos_level
):

    end = min(
        bos_index + MAX_PULLBACK_CANDLES,
        len(df)
    )

    for i in range(
        bos_index + 1,
        end
    ):

        # السعر يرجع لمنطقة BOS

        if (
            df.loc[i, "high"] >= bos_level
            and df.loc[i, "close"] < bos_level
        ):

            return i

    return None


# ============================================================
# SEARCH FOR TRADES
# ============================================================

trades = []

last_trade_index = -1


for i in range(
    50,
    len(df_m5) - 30
):

    # منع الصفقات المتداخلة
    if i <= last_trade_index:
        continue

    trend = df_m5.loc[
        i,
        "trend"
    ]

    liquidity_level = df_m5.loc[
        i,
        "liquidity_level"
    ]


    # ========================================================
    # BUY SETUP
    # M15 BUY TREND
    # M5 SWEEP BELOW M15 LIQUIDITY
    # M5 BOS UP
    # M5 PULLBACK
    # ========================================================

    if trend == "BUY":

        sweep = detect_buy_sweep(
            df_m5,
            i,
            liquidity_level
        )

        if not sweep:
            continue

        bos = detect_buy_bos(
            df_m5,
            i
        )

        if bos is None:
            continue

        pullback_index = find_buy_pullback(
            df_m5,
            bos["index"],
            bos["level"]
        )

        if pullback_index is None:
            continue


        entry = df_m5.loc[
            pullback_index,
            "close"
        ]

        stop_loss = df_m5.loc[
            i,
            "low"
        ]

        risk = entry - stop_loss

        if risk <= 0:
            continue

        take_profit = (
            entry
            + risk * RISK_REWARD
        )


        trades.append({

            "time":
                df_m5.loc[
                    pullback_index,
                    "time"
                ],

            "type":
                "BUY",

            "entry":
                entry,

            "sl":
                stop_loss,

            "tp":
                take_profit,

            "m15_liquidity":
                liquidity_level,

            "sweep_time":
                df_m5.loc[
                    i,
                    "time"
                ],

            "bos_time":
                df_m5.loc[
                    bos["index"],
                    "time"
                ]

        })

        last_trade_index = pullback_index


    # ========================================================
    # SELL SETUP
    # M15 SELL TREND
    # M5 SWEEP ABOVE M15 LIQUIDITY
    # M5 BOS DOWN
    # M5 PULLBACK
    # ========================================================

    elif trend == "SELL":

        sweep = detect_sell_sweep(
            df_m5,
            i,
            liquidity_level
        )

        if not sweep:
            continue

        bos = detect_sell_bos(
            df_m5,
            i
        )

        if bos is None:
            continue

        pullback_index = find_sell_pullback(
            df_m5,
            bos["index"],
            bos["level"]
        )

        if pullback_index is None:
            continue


        entry = df_m5.loc[
            pullback_index,
            "close"
        ]

        stop_loss = df_m5.loc[
            i,
            "high"
        ]

        risk = stop_loss - entry

        if risk <= 0:
            continue

        take_profit = (
            entry
            - risk * RISK_REWARD
        )


        trades.append({

            "time":
                df_m5.loc[
                    pullback_index,
                    "time"
                ],

            "type":
                "SELL",

            "entry":
                entry,

            "sl":
                stop_loss,

            "tp":
                take_profit,

            "m15_liquidity":
                liquidity_level,

            "sweep_time":
                df_m5.loc[
                    i,
                    "time"
                ],

            "bos_time":
                df_m5.loc[
                    bos["index"],
                    "time"
                ]

        })

        last_trade_index = pullback_index


# ============================================================
# RESULTS
# ============================================================

trades_df = pd.DataFrame(trades)

print("\n" + "=" * 60)
print("STRUCTURE + M15 LIQUIDITY + M5 SWEEP STRATEGY")
print("=" * 60)

print("\nTotal Trades:", len(trades_df))

if len(trades_df) > 0:

    buy_count = len(
        trades_df[
            trades_df["type"] == "BUY"
        ]
    )

    sell_count = len(
        trades_df[
            trades_df["type"] == "SELL"
        ]
    )

    print("BUY Trades:", buy_count)
    print("SELL Trades:", sell_count)

    print("\nLAST 10 TRADES:")
    print(
        trades_df.tail(10).to_string(
            index=False
        )
    )

    trades_df.to_csv(
        "m15_liquidity_m5_strategy.csv",
        index=False
    )

    print(
        "\nResults saved to:"
        " m15_liquidity_m5_strategy.csv"
    )

else:

    print("\nNo trades found.")


# ============================================================
# CLOSE MT5
# ============================================================

mt5.shutdown()


# ============================================================
# ============================================================
# BACKTEST — يطبّق الاستراتيجية كيما هي
# ============================================================
# ============================================================

MAX_HOLD_CANDLES = 200   # أقصى عدد شموع M5 نستنّاو فيها TP ولا SL
PIP = 0.0001             # EURUSD


def run_backtest(df, trades_df, max_hold=MAX_HOLD_CANDLES):

    if len(trades_df) == 0:
        return trades_df

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df["time"].to_list()

    time_to_index = {}
    for k, t in enumerate(times):
        time_to_index[t] = k

    results = []
    exit_times = []
    exit_prices = []
    bars_held = []
    r_values = []
    risk_pips = []

    for _, trade in trades_df.iterrows():

        entry_index = time_to_index.get(trade["time"])

        entry = trade["entry"]
        sl = trade["sl"]
        tp = trade["tp"]
        is_buy = trade["type"] == "BUY"

        risk_pips.append(
            abs(entry - sl) / PIP
        )

        result = "OPEN"
        exit_time = None
        exit_price = None
        held = 0

        if entry_index is not None:

            end = min(
                entry_index + max_hold,
                len(df)
            )

            for j in range(entry_index + 1, end):

                if is_buy:
                    hit_sl = lows[j] <= sl
                    hit_tp = highs[j] >= tp
                else:
                    hit_sl = highs[j] >= sl
                    hit_tp = lows[j] <= tp

                # الشمعة ضربت الزوز -> ناخذو الأسوأ (SL)
                if hit_sl:
                    result = "SL"
                    exit_price = sl
                elif hit_tp:
                    result = "TP"
                    exit_price = tp

                if result != "OPEN":
                    exit_time = times[j]
                    held = j - entry_index
                    break

        if result == "TP":
            r = RISK_REWARD
        elif result == "SL":
            r = -1.0
        else:
            r = 0.0

        results.append(result)
        exit_times.append(exit_time)
        exit_prices.append(exit_price)
        bars_held.append(held)
        r_values.append(r)

    out = trades_df.copy()

    out["result"] = results
    out["exit_time"] = exit_times
    out["exit_price"] = exit_prices
    out["bars_held"] = bars_held
    out["risk_pips"] = risk_pips
    out["R"] = r_values
    out["equity_R"] = out["R"].cumsum()

    return out


def print_stats(out, label="ALL"):

    closed = out[out["result"].isin(["TP", "SL"])]

    total = len(out)
    n_closed = len(closed)
    wins = len(closed[closed["result"] == "TP"])
    losses = len(closed[closed["result"] == "SL"])
    still_open = total - n_closed

    print("\n" + "-" * 60)
    print("  " + label)
    print("-" * 60)

    print("Total signals   :", total)
    print("Closed          :", n_closed)
    print("Wins (TP)       :", wins)
    print("Losses (SL)     :", losses)
    print("Never hit       :", still_open)

    if n_closed == 0:
        return

    win_rate = wins / n_closed * 100
    total_r = closed["R"].sum()
    expectancy = total_r / n_closed

    if losses > 0:
        profit_factor = (wins * RISK_REWARD) / losses
    else:
        profit_factor = float("inf")

    print("Win rate        : {:.1f} %".format(win_rate))
    print("Total R         : {:+.2f} R".format(total_r))
    print("Expectancy      : {:+.3f} R / trade".format(expectancy))
    print("Profit factor   : {:.2f}".format(profit_factor))

    print(
        "Avg risk        : {:.1f} pips".format(
            out["risk_pips"].mean()
        )
    )

    print(
        "Max risk        : {:.1f} pips".format(
            out["risk_pips"].max()
        )
    )

    print(
        "Avg bars held   : {:.0f}".format(
            closed["bars_held"].mean()
        )
    )

    # Max Drawdown بالـ R
    equity = [0.0]
    for r in closed["R"]:
        equity.append(equity[-1] + r)

    peak = equity[0]
    max_dd = 0.0

    for value in equity:
        if value > peak:
            peak = value
        dd = value - peak
        if dd < max_dd:
            max_dd = dd

    print("Max drawdown    : {:.2f} R".format(max_dd))

    # أطول سلسلة خسائر
    streak = 0
    worst_streak = 0

    for r in closed["R"]:
        if r < 0:
            streak += 1
            if streak > worst_streak:
                worst_streak = streak
        else:
            streak = 0

    print("Worst losing streak:", worst_streak)


# ============================================================
# RUN BACKTEST
# ============================================================

if len(trades_df) > 0:

    bt = run_backtest(df_m5, trades_df)

    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)

    print_stats(bt, "ALL TRADES")

    print_stats(
        bt[bt["type"] == "BUY"],
        "BUY ONLY"
    )

    print_stats(
        bt[bt["type"] == "SELL"],
        "SELL ONLY"
    )

    print("\n" + "=" * 60)
    print("EQUITY CURVE (R)")
    print("=" * 60)

    print(
        bt[[
            "time",
            "type",
            "entry",
            "sl",
            "tp",
            "risk_pips",
            "result",
            "bars_held",
            "R",
            "equity_R"
        ]].to_string(index=False)
    )

    bt.to_csv(
        "m15_liquidity_m5_backtest.csv",
        index=False
    )

    print(
        "\nSaved to: m15_liquidity_m5_backtest.csv"
    )

else:

    print("\nNo trades to backtest.")
