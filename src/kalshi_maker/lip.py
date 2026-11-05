"""Kalshi Liquidity Incentive Program (LIP) scoring, per help.kalshi.com article 13823851.

Per side (yes bids and no bids are scored separately), once a second:
- Walk down from the best bid. Reference price = first level where cumulative size
  reaches Target/5.
- Qualifying orders = the resting orders that help reach Target size on that side.
  If the side never reaches Target, nothing on it qualifies and the snapshot is excluded.
- Multiplier = 1.0 at or better than the reference price, DF ** ticks_below otherwise.
- Your side share = your raw score / all qualifying raw score.
Snapshot score = yes share + no share (max 2.0 across everyone).
Reward = your period score share x period reward x valid snapshot fraction.

Assumption: an entire price level counts once the walk reaches it (Kalshi doesn't say
whether the last level is cut off at Target).
"""

from __future__ import annotations

from dataclasses import dataclass

TICK = 0.01


@dataclass
class SideScore:
    qualifies: bool
    ref_price: float | None
    total_raw: float  # all qualifying raw score, including mine
    mine_raw: float

    @property
    def share(self) -> float:
        return self.mine_raw / self.total_raw if self.qualifies and self.total_raw else 0.0


def score_side(
    levels: list[tuple[float, float]],
    target: float,
    df: float,
    mine: tuple[float, float] | None = None,
) -> SideScore:
    """levels: (price, size) bids, any order. mine: (price, size) of my resting bid, if any.

    My order joins the back of its price level; it is scored with the level.
    """
    book: dict[float, float] = {}
    for p, s in levels:
        book[round(p, 2)] = book.get(round(p, 2), 0.0) + s
    my_p = round(mine[0], 2) if mine else None
    my_s = mine[1] if mine else 0.0
    if mine:
        book[my_p] = book.get(my_p, 0.0) + my_s

    prices = sorted(book, reverse=True)  # best bid first
    cum, ref, counted = 0.0, None, []
    for p in prices:
        cum += book[p]
        counted.append(p)
        if ref is None and cum >= target / 5:
            ref = p
        if cum >= target:
            break
    if cum < target:
        return SideScore(False, ref, 0.0, 0.0)

    def mult(p: float) -> float:
        if p >= ref - 1e-9:
            return 1.0
        return df ** round((ref - p) / TICK)

    total = sum(book[p] * mult(p) for p in counted)
    mine_raw = my_s * mult(my_p) if mine and my_p in counted else 0.0
    return SideScore(True, ref, total, mine_raw)


def reference_price(levels, target: float) -> float | None:
    return score_side(levels, target, 1.0).ref_price


def snapshot_share(book: dict, target: float, df: float, my_yes=None, my_no=None) -> tuple[float, bool]:
    """Fraction of this snapshot's reward that is mine (0..1), and whether it's valid."""
    y = score_side(book["yes"], target, df, my_yes)
    n = score_side(book["no"], target, df, my_no)
    valid = y.qualifies and n.qualifies
    if not valid:
        return 0.0, False
    return (y.share + n.share) / 2, True


def program_daily_dollars(prog: dict) -> float:
    from .api import parse_time

    days = (parse_time(prog["end_date"]) - parse_time(prog["start_date"])).total_seconds() / 86400
    return prog["period_reward"] / 10_000 / max(days, 1 / 24)  # period_reward is centi-cents


def program_df(prog: dict) -> float:
    bps = prog.get("discount_factor_bps")
    return 1.0 if bps is None else bps / 10_000  # None -> most conservative (no penalty for others)


def maker_fee_rate(fee_type: str | None, multiplier: float | None = 1.0) -> float:
    """Maker fee coefficient by series fee_type (docs.kalshi.com get-series).

    'quadratic' has no maker-fee section, so makers pay 0. Maker-fee series use 0.25x the
    0.07 taker coefficient (combo: 0.5x). The 0.07 constant is from memory of Kalshi's fee
    schedule (the PDF was rate-limited when checked 2026-11-02): verify before going live.
    """
    m = multiplier or 1.0
    if fee_type == "quadratic_with_maker_fees":
        return 0.07 * 0.25 * m
    if fee_type == "quadratic_with_combo_maker_fees":
        return 0.07 * 0.5 * m
    if fee_type == "quadratic":
        return 0.0
    return 0.07 * 0.25 * m  # unknown/flat: assume a maker fee rather than none


def maker_fee(price: float, contracts: float, rate: float) -> float:
    """Kalshi fee formula: rate x C x P x (1-P). Rate is configurable (unconfirmed for makers)."""
    return rate * contracts * price * (1 - price)
