#!/usr/bin/env python3
"""
SMC signal engine — the STRATEGY.md rules turned into bar-by-bar signals.

Every feature (swing, liquidity pool, structure event, FVG) is precomputed once
together with the bar index that *confirms* it, so a signal produced at bar i
never reads a candle after i. That is what makes the backtest honest: the engine
only knows what a trader watching the chart knew at that close.

Per bar, mirroring the six conditions in STRATEGY.md:

    1. HTF bias         resampled higher-timeframe structure (+ EMA / ADX filter)
    2. liquidity        equal highs/lows built from confirmed swings
    3. sweep            wick through the pool, close back on the other side
    4. confirmation     CHoCH on the entry timeframe within `choch_window` bars
    5. POI              order block and/or FVG inside the impulse leg
    6. premium/discount the POI must sit on the correct side of the leg's 50%

Then: entry limit on the near edge of the POI, stop beyond the sweep wick,
target the nearest resting liquidity that still pays at least `min_rr`.
"""

from collections import Counter
from dataclasses import dataclass, field

from smc_strategy import classify_structure, find_fvgs, find_liquidity_pools, find_swings
from trend_identifier import Candle, adx_series, ema


@dataclass
class Zone:
    """A point of interest — an order block, an FVG, or their overlap."""

    top: float
    bottom: float
    source: str


@dataclass
class Signal:
    direction: str  # "long" | "short"
    entry: float
    stop_loss: float
    take_profit: float
    zone: Zone
    risk: float  # price distance from entry to stop
    rr: float
    signal_index: int
    sweep_index: int
    sweep_level: float
    choch_index: int
    equilibrium: float


@dataclass
class SignalConfig:
    swing_window: int = 3
    tolerance: float = 0.0015  # equal-highs/lows tolerance, as a fraction
    htf_mult: int = 16  # entry bars per higher-timeframe bar (15m -> 4H)
    htf_swing_window: int = 2
    htf_ema: int = 50  # 0 disables the EMA agreement filter
    min_adx: float = 0.0  # 0 disables the ADX strength filter
    adx_period: int = 14
    require_bias: bool = True
    confirm: str = "choch"  # "choch" = reversal only, "any" = BOS or CHoCH
    choch_window: int = 12  # bars a sweep stays live waiting for confirmation
    min_rr: float = 3.0
    sl_buffer_pct: float = 0.1  # stop padding beyond the sweep wick, in percent
    require_pd: bool = True  # enforce the premium/discount filter


@dataclass
class _Sweep:
    index: int
    direction: str
    level: float
    extreme: float  # the wick tip — where the stop goes


@dataclass
class _Pool:
    kind: str
    level: float
    confirmed_at: int
    swept: bool = False


@dataclass
class _Feature:
    confirmed_at: int
    data: dict = field(default_factory=dict)


class SignalEngine:
    """Drive with `step(i)` for i = 0..n-1 in order; it returns a Signal on the
    bar where all six conditions line up, otherwise None. Rejections are tallied
    in `self.rejections` so a backtest can report which condition filters most."""

    def __init__(self, candles: list[Candle], config: SignalConfig | None = None):
        self.candles = candles
        self.cfg = config or SignalConfig()
        self.rejections: Counter = Counter()
        self.sweeps_seen = 0

        window = self.cfg.swing_window
        swings = find_swings(candles, window)
        for s in swings:
            s["confirmed_at"] = s["index"] + window
        self._swings = swings
        self._swing_ptr = 0
        self.confirmed_swings: list[dict] = []

        self._pools = [
            _Pool(kind=p["kind"], level=p["level"], confirmed_at=max(p["indices"]) + window)
            for p in find_liquidity_pools(swings, self.cfg.tolerance)
        ]
        self._pools.sort(key=lambda p: p.confirmed_at)
        self._pool_ptr = 0
        self._active_pools: list[_Pool] = []

        self._fvgs = [_Feature(confirmed_at=f["index"] + 1, data=f) for f in find_fvgs(candles)]
        self._fvgs.sort(key=lambda f: f.confirmed_at)
        self._fvg_ptr = 0
        self.confirmed_fvgs: list[dict] = []

        self._events_by_index: dict[int, list[dict]] = {}
        for event in classify_structure(candles, swings, window):
            self._events_by_index.setdefault(event["index"], []).append(event)

        self.htf_bias = self._build_htf_bias()
        self._live_sweeps: list[_Sweep] = []

    # ---------------------------------------------------------------- bias

    def _build_htf_bias(self) -> list[str | None]:
        """Resample the entry timeframe into higher-timeframe candles and read
        the bias off their structure. Each HTF candle only becomes usable on the
        entry bar that closes it, so `bias[i]` is always knowable at bar i."""
        n = len(self.candles)
        bias: list[str | None] = [None] * n
        if not self.cfg.require_bias:
            return ["any"] * n

        mult = max(1, self.cfg.htf_mult)
        htf: list[Candle] = []
        end_index: list[int] = []
        for start in range(0, n - mult + 1, mult):
            group = self.candles[start : start + mult]
            htf.append(
                Candle(
                    date=group[0].date,
                    open=group[0].open,
                    high=max(c.high for c in group),
                    low=min(c.low for c in group),
                    close=group[-1].close,
                )
            )
            end_index.append(start + mult - 1)

        if len(htf) < 4:
            return bias

        window = self.cfg.htf_swing_window
        events = classify_structure(htf, find_swings(htf, window), window)
        events_by_index: dict[int, dict] = {e["index"]: e for e in events}

        closes = [c.close for c in htf]
        ema_values = ema(closes, self.cfg.htf_ema) if self.cfg.htf_ema else None
        adx_values = adx_series(htf, self.cfg.adx_period) if self.cfg.min_adx > 0 else None

        current = None
        for k in range(len(htf)):
            event = events_by_index.get(k)
            if event is not None:
                current = event["direction"]

            resolved = current
            if resolved is not None and ema_values is not None:
                if k < self.cfg.htf_ema:  # EMA still seeded on too few closes
                    resolved = None
                elif (closes[k] > ema_values[k]) != (resolved == "up"):
                    resolved = None
            if resolved is not None and adx_values is not None:
                value = adx_values[k]
                if value is None or value < self.cfg.min_adx:
                    resolved = None

            stop = end_index[k + 1] if k + 1 < len(end_index) else n
            for i in range(end_index[k], stop):
                bias[i] = resolved
        return bias

    # ------------------------------------------------------------- stepping

    def step(self, i: int) -> Signal | None:
        self._advance(i)
        self._detect_sweeps(i)
        signal = self._confirm(i)
        self._expire_sweeps(i)
        return signal

    def _advance(self, i: int) -> None:
        while self._swing_ptr < len(self._swings) and self._swings[self._swing_ptr]["confirmed_at"] <= i:
            self.confirmed_swings.append(self._swings[self._swing_ptr])
            self._swing_ptr += 1
        while self._pool_ptr < len(self._pools) and self._pools[self._pool_ptr].confirmed_at <= i:
            self._active_pools.append(self._pools[self._pool_ptr])
            self._pool_ptr += 1
        while self._fvg_ptr < len(self._fvgs) and self._fvgs[self._fvg_ptr].confirmed_at <= i:
            self.confirmed_fvgs.append(self._fvgs[self._fvg_ptr].data)
            self._fvg_ptr += 1

    def _detect_sweeps(self, i: int) -> None:
        """Condition 3: a wick pierces resting liquidity and the candle closes
        back on the other side. A close *beyond* the level is a real break, not
        a sweep — the pool is simply consumed and never traded."""
        candle = self.candles[i]
        bias = self.htf_bias[i]
        for pool in self._active_pools:
            if pool.swept or pool.confirmed_at >= i:
                continue
            if pool.kind == "high" and candle.high > pool.level:
                pool.swept = True
                if candle.close >= pool.level:
                    continue
                self._open_sweep(_Sweep(i, "short", pool.level, candle.high), bias)
            elif pool.kind == "low" and candle.low < pool.level:
                pool.swept = True
                if candle.close <= pool.level:
                    continue
                self._open_sweep(_Sweep(i, "long", pool.level, candle.low), bias)

    def _open_sweep(self, sweep: _Sweep, bias: str | None) -> None:
        self.sweeps_seen += 1
        if bias is None:
            self.rejections["no_htf_bias"] += 1
            return
        wanted = "up" if sweep.direction == "long" else "down"
        if bias != "any" and bias != wanted:
            self.rejections["against_htf_bias"] += 1
            return
        self._live_sweeps.append(sweep)

    def _confirm(self, i: int) -> Signal | None:
        """Condition 4: a close that breaks entry-timeframe structure back in the
        sweep's direction."""
        events = self._events_by_index.get(i)
        if not events:
            return None
        allowed = {"CHoCH"} if self.cfg.confirm == "choch" else {"CHoCH", "BOS"}
        for event in events:
            if event["type"] not in allowed:
                continue
            wanted = "up" if event["direction"] == "up" else "down"
            for sweep in list(self._live_sweeps):
                if (sweep.direction == "long") != (wanted == "up"):
                    continue
                self._live_sweeps.remove(sweep)
                signal = self._build_setup(sweep, i)
                if signal is not None:
                    return signal
        return None

    def _expire_sweeps(self, i: int) -> None:
        for sweep in list(self._live_sweeps):
            if i - sweep.index > self.cfg.choch_window:
                self._live_sweeps.remove(sweep)
                self.rejections["no_confirmation"] += 1

    # ---------------------------------------------------------------- setup

    def _build_setup(self, sweep: _Sweep, choch_index: int) -> Signal | None:
        """Conditions 5 and 6 plus the entry/stop/target rules."""
        short = sweep.direction == "short"
        leg = self.candles[sweep.index : choch_index + 1]
        leg_high = max(c.high for c in leg)
        leg_low = min(c.low for c in leg)
        equilibrium = (leg_high + leg_low) / 2

        zone = self._find_zone(sweep.index, choch_index, short)
        if zone is None:
            self.rejections["no_poi"] += 1
            return None

        # Entry sits on the edge the price reaches first on its way back.
        entry = zone.bottom if short else zone.top

        if self.cfg.require_pd and ((entry <= equilibrium) if short else (entry >= equilibrium)):
            self.rejections["wrong_pd_zone"] += 1
            return None

        buffer = 1 + self.cfg.sl_buffer_pct / 100
        if short:
            stop_loss = max(sweep.extreme, zone.top) * buffer
        else:
            stop_loss = min(sweep.extreme, zone.bottom) * (2 - buffer)

        risk = (stop_loss - entry) if short else (entry - stop_loss)
        if risk <= 0:
            self.rejections["invalid_stop"] += 1
            return None

        close = self.candles[choch_index].close
        if (close >= entry) if short else (close <= entry):
            self.rejections["entry_already_passed"] += 1
            return None

        target = self._find_target(entry, risk, short)
        if target is None:
            self.rejections["rr_below_minimum"] += 1
            return None

        reward = (entry - target) if short else (target - entry)
        return Signal(
            direction=sweep.direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
            zone=zone,
            risk=risk,
            rr=reward / risk,
            signal_index=choch_index,
            sweep_index=sweep.index,
            sweep_level=sweep.level,
            choch_index=choch_index,
            equilibrium=equilibrium,
        )

    def _find_zone(self, start: int, end: int, short: bool) -> Zone | None:
        """The order block (last opposite-coloured candle before the impulse) and
        the FVG left behind by that impulse. Their overlap is the A+ zone; either
        one alone still counts."""
        ob = None
        for j in range(end, start - 1, -1):
            candle = self.candles[j]
            bullish = candle.close >= candle.open
            if bullish == short:  # last up-candle before a drop, or vice versa
                ob = Zone(top=candle.high, bottom=candle.low, source="OB")
                break

        kind = "bearish" if short else "bullish"
        in_leg = [f for f in self.confirmed_fvgs if f["kind"] == kind and start <= f["index"] <= end]
        fvg = None
        if in_leg:
            overlapping = [
                f for f in in_leg if ob is not None and f["bottom"] < ob.top and f["top"] > ob.bottom
            ]
            chosen = overlapping[-1] if overlapping else in_leg[-1]
            fvg = Zone(top=chosen["top"], bottom=chosen["bottom"], source="FVG")

        if ob is not None and fvg is not None and fvg.bottom < ob.top and fvg.top > ob.bottom:
            return Zone(top=min(ob.top, fvg.top), bottom=max(ob.bottom, fvg.bottom), source="OB+FVG")
        return ob or fvg

    def _find_target(self, entry: float, risk: float, short: bool) -> float | None:
        """Condition on the exit: aim at resting liquidity, not at a round number.
        Walk the confirmed swings outward from the entry and take the first level
        that still pays `min_rr`; if none does, there is no trade."""
        kind = "low" if short else "high"
        levels = sorted(
            {s["price"] for s in self.confirmed_swings if s["kind"] == kind and ((s["price"] < entry) if short else (s["price"] > entry))},
            reverse=short,
        )
        needed = self.cfg.min_rr * risk
        for level in levels:
            if ((entry - level) if short else (level - entry)) >= needed:
                return level
        return None

    def last_swing(self, kind: str, before: int) -> float | None:
        """Most recent confirmed swing of `kind` — used for the trailing stop."""
        for s in reversed(self.confirmed_swings):
            if s["kind"] == kind and s["confirmed_at"] <= before:
                return s["price"]
        return None
