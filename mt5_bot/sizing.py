"""Turning a dollar target into broker prices.

This is the heart of the "$1 to $3 a trade" requirement: the bot never guesses
pip distances, it converts the money target into a price distance using the
broker's own tick value, then validates that distance against the broker's
minimum stop distance and the current spread.
"""

from dataclasses import dataclass

from .config import Config
from .models import SymbolSpec


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry: float
    sl: float
    tp: float
    volume: float
    target_usd: float
    risk_usd: float
    target_points: float
    stop_points: float


def points_for_money(spec: SymbolSpec, volume: float, money: float) -> float:
    """How far price must travel, in points, to be worth `money`."""
    per_point = spec.money_per_point(volume)
    if per_point <= 0:
        raise ValueError(f"{spec.name}: broker reported a non-positive point value")
    return money / per_point


def money_for_points(spec: SymbolSpec, volume: float, points: float) -> float:
    return points * spec.money_per_point(volume)


def choose_target_usd(cfg: Config, spec: SymbolSpec, volume: float, atr_price: float) -> float:
    """Pick this trade's dollar target inside the configured band.

    A quiet market cannot deliver $3 in a few minutes, so the target scales with
    ATR and is then clamped into [min, max]. That keeps trades fast instead of
    leaving them hanging for hours waiting on a target the session cannot pay.
    """
    atr_points = atr_price / spec.point
    atr_money = money_for_points(spec, volume, atr_points)
    scaled = atr_money * cfg.atr_target_fraction
    return max(cfg.target_profit_min_usd, min(scaled, cfg.target_profit_max_usd))


def build_plan(
    cfg: Config,
    spec: SymbolSpec,
    side: str,
    bid: float,
    ask: float,
    atr_price: float,
    volume: float,
) -> tuple[TradePlan | None, str]:
    """Build a fully-specified order, or explain why the trade is not takeable."""
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    target_usd = choose_target_usd(cfg, spec, volume, atr_price)
    commission = cfg.commission_per_lot_usd * volume
    # Commission is paid on winners and losers alike, so it is added to the
    # distance price must travel to pay the target, and subtracted from the
    # distance allowed before the stop - both figures below are then net.
    gross_target = target_usd + commission
    risk_usd = min(target_usd * cfg.stop_loss_ratio, cfg.max_loss_per_trade_usd)
    if risk_usd <= commission:
        return None, (
            f"commission (${commission:.2f}) leaves nothing of the ${risk_usd:.2f} "
            "risk budget - raise `max_loss_per_trade_usd`"
        )

    target_points = points_for_money(spec, volume, gross_target)
    stop_points = points_for_money(spec, volume, risk_usd - commission)

    spread_points = (ask - bid) / spec.point
    if spread_points > cfg.max_spread_points:
        return None, f"spread {spread_points:.1f}p above limit {cfg.max_spread_points:.1f}p"
    if target_points <= 0:
        return None, "target distance computed as zero"
    if spread_points / target_points > cfg.max_spread_to_target_ratio:
        return None, (
            f"spread {spread_points:.1f}p is {spread_points / target_points:.0%} of the "
            f"{target_points:.1f}p target - lower `volume`, raise the target, or pick a "
            "tighter-spread symbol"
        )

    atr_points = atr_price / spec.point
    if atr_points > 0 and target_points > atr_points * cfg.max_target_atr_multiple:
        return None, (
            f"target {target_points:.1f}p is {target_points / atr_points:.1f} ATRs away - "
            "too slow to scalp; lower `target_profit_min_usd` or raise `volume`"
        )

    # The broker refuses SL/TP closer than stops_level; widening the TP would
    # silently break the dollar target, so the trade is skipped instead.
    min_distance = max(spec.stops_level_points, spec.freeze_level_points)
    if target_points < min_distance:
        return None, (
            f"target {target_points:.1f}p is inside the broker's {min_distance}p stop level "
            "- lower `volume` or raise `target_profit_min_usd`"
        )
    stop_points = max(stop_points, float(min_distance))
    net_risk = money_for_points(spec, volume, stop_points) + commission
    if net_risk > cfg.max_loss_per_trade_usd:
        return None, (
            f"broker's {min_distance}p minimum stop would risk ${net_risk:.2f}, over the "
            f"${cfg.max_loss_per_trade_usd:.2f} cap"
        )

    entry = ask if side == "buy" else bid
    offset_tp = target_points * spec.point
    offset_sl = stop_points * spec.point
    if side == "buy":
        tp, sl = entry + offset_tp, entry - offset_sl
    else:
        tp, sl = entry - offset_tp, entry + offset_sl

    return (
        TradePlan(
            side=side,
            entry=spec.round_price(entry),
            sl=spec.round_price(sl),
            tp=spec.round_price(tp),
            volume=volume,
            target_usd=target_usd,
            risk_usd=net_risk,
            target_points=target_points,
            stop_points=stop_points,
        ),
        "",
    )
