"""Pre-flight check: can this broker actually pay $1-$3 a trade?

The dollar target and the lot size are two ends of the same lever. Bigger lots
make the target closer, which is faster but drowns it in the spread; smaller
lots push the target further away than the session's volatility can reach. This
module solves for the lot size where both ends hold, and says plainly when no
such size exists on a given symbol.
"""

from dataclasses import dataclass

from .config import Config
from .models import SymbolSpec
from .sizing import points_for_money


@dataclass(frozen=True)
class Viability:
    symbol: str
    spread_points: float
    atr_points: float
    target_usd: float
    min_target_points: float  # below this the spread eats the trade
    max_target_points: float  # above this the trade is too slow to be a scalp
    volume_low: float
    volume_high: float
    suggested_volume: float
    ok: bool
    note: str


def assess(cfg: Config, spec: SymbolSpec, spread_points: float, atr_points: float) -> Viability:
    """Work out the lot range that keeps a `target_profit_min_usd` trade viable."""
    target_usd = cfg.target_profit_min_usd
    per_point_one_lot = spec.money_per_point(1.0)

    broker_floor = float(max(spec.stops_level_points, spec.freeze_level_points))
    spread_floor = spread_points / cfg.max_spread_to_target_ratio if cfg.max_spread_to_target_ratio else 0.0
    min_target_points = max(broker_floor, spread_floor, 1e-9)
    max_target_points = atr_points * cfg.max_target_atr_multiple

    # volume = target / (value per point per lot x distance) - distance and
    # volume move in opposite directions, so the *near* limit gives the max lot.
    volume_high = target_usd / (per_point_one_lot * min_target_points)
    volume_low = (
        target_usd / (per_point_one_lot * max_target_points) if max_target_points > 0 else float("inf")
    )

    if volume_low > volume_high:
        return Viability(
            spec.name, spread_points, atr_points, target_usd,
            min_target_points, max_target_points, volume_low, volume_high, 0.0, False,
            f"No lot size works: ${target_usd:.2f} needs at least {min_target_points:.0f} points to "
            f"clear the spread, but {atr_points:.0f}-point volatility only reaches "
            f"{max_target_points:.0f}. Trade a tighter-spread symbol, wait for a livelier session, "
            f"or accept a smaller target.",
        )

    # Sit in the middle of the workable band, then respect the broker's lot grid.
    suggested = spec.round_volume((volume_low * volume_high) ** 0.5)
    ok = spec.volume_min <= suggested <= spec.volume_max and volume_high >= spec.volume_min
    note = (
        f"Trade {suggested:.2f} lot: ${target_usd:.2f} lands about "
        f"{points_for_money(spec, suggested, target_usd):.0f} points away, against a "
        f"{spread_points:.0f}-point spread and {atr_points:.0f}-point ATR."
    )
    if not ok:
        note = (
            f"The workable range ({volume_low:.3f}-{volume_high:.3f} lot) falls outside the "
            f"broker's {spec.volume_min:.2f}-{spec.volume_max:.2f} lot limits."
        )
    return Viability(
        spec.name, spread_points, atr_points, target_usd, min_target_points,
        max_target_points, volume_low, volume_high, suggested, ok, note,
    )


def report(cfg: Config, viability: Viability) -> str:
    lines = [
        f"--- {viability.symbol} ---",
        f"  spread            : {viability.spread_points:.1f} points",
        f"  ATR({cfg.atr_period})           : {viability.atr_points:.1f} points",
        f"  target            : ${viability.target_usd:.2f} "
        f"({viability.min_target_points:.0f}-{viability.max_target_points:.0f} points usable)",
        f"  configured volume : {cfg.volume:.2f} lot",
        f"  workable volume   : {viability.volume_low:.3f} - {viability.volume_high:.3f} lot",
        f"  suggested volume  : {viability.suggested_volume:.2f} lot"
        if viability.ok
        else "  suggested volume  : none",
        f"  verdict           : {'OK' if viability.ok else 'NOT VIABLE'}",
        f"  {viability.note}",
    ]
    if viability.ok and not (viability.volume_low <= cfg.volume <= viability.volume_high):
        lines.append(
            f"  WARNING: the configured {cfg.volume:.2f} lot is outside that range, so most "
            "signals will be skipped."
        )
    return "\n".join(lines)
