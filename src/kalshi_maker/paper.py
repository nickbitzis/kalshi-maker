"""Paper-trade the liquidity-reward strategy against live Kalshi books. No money, no API key.

    uv run kalshi-paper --markets 20 --size 250 --hours 10

Each cycle (default 5 s):
1. Pull all chosen order books in one batched call.
2. Keep a virtual yes bid and no bid resting at each side's reference price
   (re-quote when it moves; a re-quote goes to the back of the queue).
3. Accrue the reward we'd earn this cycle from our share of qualifying depth.
4. Simulate fills from the public trade tape: a trade through our price fills us; a trade
   at our price fills us only after the size that was queued ahead of us.
5. Inventory: if one side fills, stop re-posting that side until the other side pairs it
   off (a yes + no pair settles at exactly $1). Positions are marked at the mid.
Writes data/paper/<run>/cycles.jsonl, fills.jsonl and state.json; `kalshi-report` summarizes.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .api import Kalshi, parse_time
from .lip import maker_fee, maker_fee_rate, program_daily_dollars, program_df, score_side, snapshot_share
from .scan import DATA, scan


@dataclass
class Quote:
    price: float | None = None
    size: float = 0.0  # remaining resting size
    queue_ahead: float = 0.0


@dataclass
class Book:
    ticker: str
    daily: float
    target: float
    df: float
    fee_rate: float
    size: float
    yes: Quote = field(default_factory=Quote)
    no: Quote = field(default_factory=Quote)
    pos_yes: float = 0.0  # contracts held
    pos_no: float = 0.0
    cost_yes: float = 0.0  # dollars paid for open (unpaired) inventory
    cost_no: float = 0.0
    realized: float = 0.0  # locked pair profit
    fees: float = 0.0
    reward: float = 0.0
    valid_s: float = 0.0
    total_s: float = 0.0
    fills: int = 0
    last_trade_ts: int = 0
    mid_yes: float | None = None

    @property
    def cost(self) -> float:
        return self.cost_yes + self.cost_no

    def mtm(self) -> float:
        if self.mid_yes is None:
            return -self.cost
        return self.pos_yes * self.mid_yes + self.pos_no * (1 - self.mid_yes) - self.cost

    def net(self) -> float:
        return self.reward + self.realized + self.mtm() - self.fees


def level_size(levels, price) -> float:
    return sum(s for p, s in levels if abs(p - price) < 1e-9)


MIN_PRICE = 0.05


def requote(b: Book, side: str, levels, max_inventory: float):
    q: Quote = getattr(b, side)
    held = b.pos_yes - b.pos_no if side == "yes" else b.pos_no - b.pos_yes
    if held >= max_inventory:  # already long this side: let the other side pair it off
        q.price, q.size = None, 0.0
        return
    s = score_side(levels, b.target, b.df)
    price = s.ref_price if s.ref_price is not None else (max(p for p, _ in levels) if levels else None)
    if price is None or not (MIN_PRICE <= price <= 1 - MIN_PRICE):  # near-resolved: stop quoting
        q.price, q.size = None, 0.0
        return
    # holding the other side: never bid so high that the pair costs more than $1
    other_qty, other_cost = (b.pos_no, b.cost_no) if side == "yes" else (b.pos_yes, b.cost_yes)
    if other_qty > 0 and price + other_cost / other_qty >= 1:
        q.price, q.size = None, 0.0
        return
    if q.price is None or abs(q.price - price) > 1e-9 or q.size <= 0:
        q.price, q.size = price, b.size
        q.queue_ahead = level_size(levels, price)  # we join the back of the level


def apply_fill(b: Book, side: str, qty: float, price: float, log):
    q: Quote = getattr(b, side)
    qty = min(qty, q.size)
    if qty <= 0:
        return
    q.size -= qty
    fee = maker_fee(price, qty, b.fee_rate)
    b.fees += fee
    b.fills += 1
    if side == "yes":
        b.pos_yes += qty
        b.cost_yes += qty * price
    else:
        b.pos_no += qty
        b.cost_no += qty * price
    # pair off yes + no -> each pair pays exactly $1 at settlement
    pair = min(b.pos_yes, b.pos_no)
    if pair > 0:
        avg_y, avg_n = b.cost_yes / b.pos_yes, b.cost_no / b.pos_no
        b.realized += pair * (1 - avg_y - avg_n)
        b.cost_yes -= pair * avg_y
        b.cost_no -= pair * avg_n
        b.pos_yes -= pair
        b.pos_no -= pair
    log({"t": time.time(), "ticker": b.ticker, "side": side, "qty": qty, "price": price, "fee": round(fee, 4)})


def process_trades(b: Book, trades: list[dict], log):
    for t in sorted(trades, key=lambda t: t["created_time"]):
        ts = int(parse_time(t["created_time"]).timestamp() * 1000)
        if ts <= b.last_trade_ts:
            continue
        b.last_trade_ts = ts
        n = float(t["count_fp"])
        # taker bought NO -> hit a resting YES bid at yes_price; taker bought YES -> hit a NO bid
        side = "yes" if t["taker_side"] == "no" else "no"
        px = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
        q: Quote = getattr(b, side)
        if q.price is None or q.size <= 0 or px > q.price + 1e-9:
            continue
        if px < q.price - 1e-9:  # traded through our level: we were filled first
            apply_fill(b, side, n, q.price, log)
        else:
            eat = min(n, q.queue_ahead)
            q.queue_ahead -= eat
            if n - eat > 0:
                apply_fill(b, side, n - eat, q.price, log)


# News-driven categories: informed traders pick off resting bids right before a move.
# Live run 1: election, executive-order and Trump-appearance markets lost $21; slow two-way markets made money.
EXCLUDE_CATEGORIES = {"Elections", "Politics"}
MIN_HOURS_TO_CLOSE = 7 * 24  # this-week markets resolve on news; we hold one side when it lands
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
DATE_CODE = re.compile(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})")


def event_date(ticker: str) -> date | None:
    m = DATE_CODE.search(ticker)
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def risky(ticker: str, today: date | None = None) -> bool:
    """Event date already reached: the outcome may be known but not yet settled."""
    d = event_date(ticker)
    return d is not None and d <= (today or datetime.now(timezone.utc).date())


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


MIN_PERIOD_PAYOUT = 2.0  # dollars; Kalshi's floor is $1 per program period, keep a margin


def period_days(prog: dict) -> float:
    return (parse_time(prog["end_date"]) - parse_time(prog["start_date"])).total_seconds() / 86400


def pick_markets(k: Kalshi, n: int, size: float, max_capital: float) -> list[Book]:
    rows, _ = scan(size=size, min_hours=MIN_HOURS_TO_CLOSE)
    progs = {p["market_ticker"]: p for p in k.incentives()}
    books, cap, events = [], 0.0, set()
    for r in rows:
        if risky(r["ticker"]) or event_of(r["ticker"]) in events:  # one market per event: strikes move together
            continue
        # skip sub-5c sides: tiny capital but the reward pools there are small and illiquid
        if min(r["yes_bid"], r["no_bid"]) < 0.05 or r["exp_per_day"] < 1:
            continue
        if cap + r["capital"] > max_capital:
            continue
        p = progs[r["ticker"]]
        # Kalshi does not pay a program's reward under $1 per period: small shares are forfeited
        if r["exp_per_day"] * period_days(p) < MIN_PERIOD_PAYOUT:
            continue
        series = k.get(f"/series/{r['ticker'].split('-')[0]}")["series"]
        if series.get("category") in EXCLUDE_CATEGORIES:
            continue
        books.append(Book(
            ticker=r["ticker"], daily=program_daily_dollars(p), target=float(p["target_size_fp"]),
            df=program_df(p), fee_rate=maker_fee_rate(series.get("fee_type"), series.get("fee_multiplier")),
            size=size,
        ))
        cap += r["capital"]
        events.add(event_of(r["ticker"]))
        if len(books) >= n:
            break
    return books


def run(n_markets: int, size: float, hours: float, interval: float, max_capital: float, max_inventory: float):
    k = Kalshi()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = DATA / "paper" / run_id
    out.mkdir(parents=True, exist_ok=True)
    books = pick_markets(k, n_markets, size, max_capital)
    start_ms = int(time.time() * 1000)
    for b in books:
        b.last_trade_ts = start_ms
    (out / "config.json").write_text(json.dumps({
        "markets": [b.ticker for b in books], "size": size, "hours": hours, "interval": interval,
        "max_capital": max_capital, "max_inventory": max_inventory, "started": run_id,
    }, indent=2))
    print(f"paper run {run_id}: {len(books)} markets, size {size:g}/side -> {out}", flush=True)
    fills_f = (out / "fills.jsonl").open("a")
    cycles_f = (out / "cycles.jsonl").open("a")
    log_fill = lambda d: (fills_f.write(json.dumps(d) + "\n"), fills_f.flush())
    end = time.time() + hours * 3600
    last = time.time()
    while time.time() < end:
        t0 = time.time()
        dt, last = t0 - last, t0
        try:
            obs = k.orderbooks([b.ticker for b in books])
            for b in books:
                ob = obs.get(b.ticker)
                if not ob:
                    continue
                # 1) score the snapshot with the quotes that rested through the last interval
                my_y = (b.yes.price, b.yes.size) if b.yes.price is not None and b.yes.size > 0 else None
                my_n = (b.no.price, b.no.size) if b.no.price is not None and b.no.size > 0 else None
                share, valid = snapshot_share(ob, b.target, b.df, my_y, my_n)
                b.total_s += dt
                if valid:
                    b.valid_s += dt
                    b.reward += b.daily / 86400 * dt * share
                # 2) fills since last cycle
                process_trades(b, k.trades(b.ticker, min_ts=b.last_trade_ts // 1000), log_fill)
                # 3) mark + re-quote
                if ob["yes"] and ob["no"]:
                    best_y, best_n = max(p for p, _ in ob["yes"]), max(p for p, _ in ob["no"])
                    b.mid_yes = (best_y + (1 - best_n)) / 2
                requote(b, "yes", ob["yes"], max_inventory)
                requote(b, "no", ob["no"], max_inventory)
            tot = {
                "t": t0, "reward": sum(b.reward for b in books), "realized": sum(b.realized for b in books),
                "mtm": sum(b.mtm() for b in books), "fees": sum(b.fees for b in books),
                "net": sum(b.net() for b in books), "fills": sum(b.fills for b in books),
                "resting_capital": sum((b.yes.price or 0) * b.yes.size + (b.no.price or 0) * b.no.size for b in books),
                "inventory_cost": sum(b.cost for b in books),
            }
            cycles_f.write(json.dumps(tot) + "\n")
            cycles_f.flush()
            (out / "state.json").write_text(json.dumps([asdict(b) for b in books], indent=1))
        except Exception as e:  # network blips: log and keep going
            print(f"cycle error: {e!r}", flush=True)
        time.sleep(max(0.0, interval - (time.time() - t0)))
    print(f"done: {out}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=20)
    ap.add_argument("--size", type=float, default=250, help="contracts per side")
    ap.add_argument("--hours", type=float, default=10)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--max-capital", type=float, default=3000, help="total resting dollars")
    ap.add_argument("--max-inventory", type=float, default=250, help="max unpaired contracts per market")
    a = ap.parse_args()
    run(a.markets, a.size, a.hours, a.interval, a.max_capital, a.max_inventory)


if __name__ == "__main__":
    main()
