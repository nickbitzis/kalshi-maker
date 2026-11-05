"""Rank active Kalshi liquidity-reward markets by expected reward per dollar tied up.

    uv run kalshi-scan --size 250 --top 30

For each market we imagine resting `size` contracts on BOTH sides (a yes bid and a no bid)
at that side's reference price (multiplier 1.0, but usually behind the best bid, so fewer
fills), then compute our share of the reward from the live order book.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .api import Kalshi, parse_time
from .lip import maker_fee, program_daily_dollars, program_df, score_side, snapshot_share

DATA = Path(__file__).resolve().parents[2] / "data"


def quote_price(levels, target, df):
    """Price to rest at on one side: the reference price, or the best bid if the side is thin."""
    if not levels:
        return None
    s = score_side(levels, target, df)
    if s.ref_price is not None:
        return s.ref_price
    return max(p for p, _ in levels)


def evaluate(prog: dict, book: dict, market: dict | None, size: float, fee_rate: float) -> dict | None:
    target = float(prog["target_size_fp"])
    df = program_df(prog)
    py = quote_price(book["yes"], target, df)
    pn = quote_price(book["no"], target, df)
    if py is None or pn is None or py + pn >= 1:
        return None
    share, valid = snapshot_share(book, target, df, my_yes=(py, size), my_no=(pn, size))
    if not valid:
        return None
    daily = program_daily_dollars(prog)
    exp = daily * share
    capital = size * (py + pn)
    best_y = max(p for p, _ in book["yes"])
    best_n = max(p for p, _ in book["no"])
    hours_left = None
    if market and market.get("close_time"):
        hours_left = (parse_time(market["close_time"]) - datetime.now(timezone.utc)).total_seconds() / 3600
    # if both sides filled once: locked $1 per pair minus what we paid, minus fees
    pair_edge = size * (1 - py - pn) - maker_fee(py, size, fee_rate) - maker_fee(pn, size, fee_rate)
    return {
        "ticker": prog["market_ticker"],
        "title": (market or {}).get("title", ""),
        "daily_reward_pool": round(daily, 2),
        "exp_per_day": round(exp, 2),
        "share": round(share, 4),
        "capital": round(capital, 2),
        "roi_per_day_pct": round(100 * exp / capital, 3) if capital else 0,
        "yes_bid": py,
        "no_bid": pn,
        "yes_ticks_behind": round((best_y - py) / 0.01),
        "no_ticks_behind": round((best_n - pn) / 0.01),
        "pair_edge_if_both_fill": round(pair_edge, 2),
        "volume_24h": float((market or {}).get("volume_24h_fp") or 0),
        "hours_to_close": round(hours_left, 1) if hours_left is not None else None,
        "period_end": prog["end_date"],
        "target": target,
    }


def markets_by_ticker(k: Kalshi, tickers: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(tickers), 100):
        d = k.get("/markets", {"tickers": ",".join(tickers[i : i + 100]), "limit": 1000})
        for m in d.get("markets", []):
            out[m["ticker"]] = m
    return out


def scan(size: float = 250, fee_rate: float = 0.0175, min_hours: float = 48) -> list[dict]:
    k = Kalshi()
    now = datetime.now(timezone.utc)
    progs = [p for p in k.incentives() if parse_time(p["start_date"]) <= now < parse_time(p["end_date"])]
    tickers = sorted({p["market_ticker"] for p in progs})
    books = k.orderbooks(tickers)
    markets = markets_by_ticker(k, tickers)
    rows = []
    for p in progs:
        b = books.get(p["market_ticker"])
        if not b:
            continue
        r = evaluate(p, b, markets.get(p["market_ticker"]), size, fee_rate)
        if r and (r["hours_to_close"] is None or r["hours_to_close"] >= min_hours):
            rows.append(r)
    rows.sort(key=lambda r: r["roi_per_day_pct"], reverse=True)
    return rows, len(progs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=250, help="contracts per side")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--fee-rate", type=float, default=0.0175, help="maker fee coefficient (unconfirmed)")
    ap.add_argument("--min-hours", type=float, default=48, help="skip markets closing sooner (news/adverse risk)")
    a = ap.parse_args()
    rows, n = scan(a.size, a.fee_rate, a.min_hours)
    DATA.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    path = DATA / f"scan_{stamp}_size{int(a.size)}.csv"
    if rows:
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"{n} active reward programs, {len(rows)} quotable at size {a.size:g}/side. Saved {path.name}")
    cols = ["ticker", "exp_per_day", "capital", "roi_per_day_pct", "daily_reward_pool", "share",
            "yes_bid", "no_bid", "yes_ticks_behind", "no_ticks_behind", "volume_24h", "hours_to_close"]
    print(" | ".join(cols))
    for r in rows[: a.top]:
        print(" | ".join(str(r[c]) for c in cols))
    tot = sum(r["exp_per_day"] for r in rows[: a.top])
    cap = sum(r["capital"] for r in rows[: a.top])
    print(f"\nTop {a.top}: ${tot:.2f}/day expected on ${cap:,.0f} resting (before fills and competition changes)")


if __name__ == "__main__":
    main()
