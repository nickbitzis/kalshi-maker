"""Live liquidity-reward bot. Same quoting rules as `paper.py`, but with real orders.

    uv run kalshi-bot --max-capital 300 --size 25                 # DRY RUN: reads your account, sends nothing
    uv run kalshi-bot --max-capital 300 --size 25 --place         # real orders

Each cycle (default 5 s):
1. Read public order books, your resting orders and your positions.
2. Take your own orders out of the book, then work out where each side should rest
   (reference price, 5-95c band, inventory cap, never a pair above $1 -- see `paper.requote`).
3. Reconcile: keep an order already at the right price, otherwise cancel it and post a new one.
   Orders are post-only, so they can never take liquidity or pay taker fees.

Safety:
- `--max-capital`: resting dollars + held inventory never exceed this.
- `--max-loss`: if balance + portfolio value falls this far below the start, cancel all and stop.
- Kill switch: create the file `data/STOP` and the bot cancels everything and exits.
- Any exit (Ctrl-C, kill, crash) cancels all resting orders.
Logs: data/live/<run>/actions.jsonl and cycles.jsonl.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import datetime, timezone

from .api import Kalshi
from .paper import Book, Quote, pick_markets, requote
from .scan import DATA

STOP_FILE = DATA / "STOP"


def remove_mine(levels, price, size):
    """Order book levels with our own resting size taken out (so we don't chase ourselves)."""
    if price is None or size <= 0:
        return levels
    out = []
    for p, s in levels:
        if abs(p - price) < 1e-9:
            s -= size
        if s > 1e-9:
            out.append((p, s))
    return out


def my_orders(resting: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """ticker -> {"yes": [orders], "no": [orders]} with price in that side's dollars."""
    out: dict[str, dict[str, list[dict]]] = {}
    for o in resting:
        side = "yes" if o["book_side"] == "bid" else "no"
        yes_px = float(o["yes_price_dollars"])
        out.setdefault(o["ticker"], {"yes": [], "no": []})[side].append({
            "id": o["order_id"],
            "price": round(yes_px if side == "yes" else 1 - yes_px, 4),
            "remaining": float(o["remaining_count_fp"]),
        })
    return out


def apply_position(b: Book, pos: dict | None):
    """Kalshi nets yes against no, so a market holds one signed position: + YES, - NO."""
    b.pos_yes = b.pos_no = b.cost_yes = b.cost_no = 0.0
    if not pos:
        return
    n, cost = float(pos["position_fp"]), abs(float(pos["market_exposure_dollars"]))
    if n > 0:
        b.pos_yes, b.cost_yes = n, cost
    elif n < 0:
        b.pos_no, b.cost_no = -n, cost


def plan(b: Book, book: dict, mine: dict[str, list[dict]], max_inventory: float) -> list[tuple]:
    """Actions for one market: ("cancel", side, order_id) / ("place", side, price, size)."""
    actions = []
    for side in ("yes", "no"):
        orders = mine.get(side, [])
        levels = book[side]
        for o in orders:
            levels = remove_mine(levels, o["price"], o["remaining"])
        setattr(b, side, Quote())
        requote(b, side, levels, max_inventory)
        want: Quote = getattr(b, side)
        keep = None
        if want.price is not None:
            keep = next((o for o in orders if abs(o["price"] - want.price) < 1e-9), None)
        for o in orders:
            if o is not keep:
                actions.append(("cancel", side, o["id"]))
        if want.price is not None and keep is None:
            actions.append(("place", side, want.price, want.size))
    return actions


class Bot:
    def __init__(self, auth, public: Kalshi, books: list[Book], max_capital: float, max_inventory: float,
                 max_loss: float, place: bool, out_dir, stop_file=STOP_FILE):
        self.auth, self.public, self.books = auth, public, books
        self.max_capital, self.max_inventory, self.max_loss = max_capital, max_inventory, max_loss
        self.place, self.out, self.stop_file = place, out_dir, stop_file
        self.start_value: int | None = None
        out_dir.mkdir(parents=True, exist_ok=True)
        self._actions = (out_dir / "actions.jsonl").open("a")
        self._cycles = (out_dir / "cycles.jsonl").open("a")

    def log(self, f, d):
        f.write(json.dumps({"t": time.time(), **d}) + "\n")
        f.flush()

    def stop_reason(self, value: int) -> str | None:
        if self.stop_file.exists():
            return f"kill switch {self.stop_file}"
        if self.start_value is not None and (self.start_value - value) / 100 >= self.max_loss:
            return f"max loss: down ${(self.start_value - value) / 100:.2f}"
        return None

    def cycle(self) -> str | None:
        value = self.auth.balance_cents()
        if self.start_value is None:
            self.start_value = value
        if reason := self.stop_reason(value):
            return reason
        if not self.public.trading_active():  # exchange closed: orders would be rejected, wait
            self.log(self._cycles, {"value_cents": value, "start_cents": self.start_value, "paused": "exchange closed"})
            return None
        tickers = [b.ticker for b in self.books]
        obs = self.public.orderbooks(tickers)
        mine = my_orders([o for o in self.auth.resting_orders() if o["ticker"] in tickers])
        positions = {p["ticker"]: p for p in self.auth.market_positions()}
        inventory = 0.0
        plans = []
        for b in self.books:
            apply_position(b, positions.get(b.ticker))
            inventory += b.cost
            ob = obs.get(b.ticker)
            if ob:
                plans.append((b, plan(b, ob, mine.get(b.ticker, {}), self.max_inventory)))
        # capital: everything we keep resting + inventory + new orders must fit under the cap
        resting = sum(o["price"] * o["remaining"] for m in mine.values() for s in m.values() for o in s)
        cancelled = {a[2] for _, acts in plans for a in acts if a[0] == "cancel"}
        resting -= sum(o["price"] * o["remaining"] for m in mine.values() for s in m.values() for o in s
                       if o["id"] in cancelled)
        used = resting + inventory
        for b, acts in plans:
            for a in acts:
                if a[0] == "cancel":
                    self.do(b.ticker, a)
            for a in acts:
                if a[0] == "place":
                    cost = a[2] * a[3]
                    if used + cost > self.max_capital:
                        self.log(self._actions, {"skip": "capital", "ticker": b.ticker, "side": a[1]})
                        continue
                    used += cost
                    self.do(b.ticker, a)
        self.log(self._cycles, {"value_cents": value, "start_cents": self.start_value,
                                "resting": round(resting, 2), "inventory": round(inventory, 2), "used": round(used, 2)})
        return None

    def do(self, ticker: str, a: tuple):
        rec = {"ticker": ticker, "action": a[0], "side": a[1], "sent": self.place}
        if a[0] == "cancel":
            rec["order_id"] = a[2]
            if self.place:
                self.auth.cancel(a[2], ticker)
        else:
            _, side, price, size = a
            # yes bid = "bid" at the yes price; no bid = "ask" (sell YES) at 1 - no price
            book_side, yes_px = ("bid", price) if side == "yes" else ("ask", round(1 - price, 4))
            rec.update(price=price, size=size, book_side=book_side, yes_price=yes_px)
            if self.place:
                try:
                    rec["order_id"] = self.auth.create_order(ticker, book_side, yes_px, size).get("order_id")
                except Exception as e:  # e.g. post-only would cross: skip this cycle, retry next
                    rec["error"] = repr(e)
        self.log(self._actions, rec)

    def shutdown(self, reason: str):
        print(f"stopping: {reason}", flush=True)
        if self.place:
            self.auth.cancel_all()
        self.log(self._actions, {"action": "cancel_all", "reason": reason, "sent": self.place})

    def run(self, hours: float, interval: float):
        def on_term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, on_term)
        end = time.time() + hours * 3600
        reason = "time up"
        try:
            while time.time() < end:
                t0 = time.time()
                try:
                    if r := self.cycle():
                        reason = r
                        break
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # network blips: log and keep going
                    print(f"cycle error: {e!r}", flush=True)
                time.sleep(max(0.0, interval - (time.time() - t0)))
        except KeyboardInterrupt:
            reason = "interrupted"
        finally:
            self.shutdown(reason)


def main():
    from .live import AuthKalshi

    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=10)
    ap.add_argument("--size", type=float, default=25, help="contracts per side")
    ap.add_argument("--max-capital", type=float, default=300, help="resting + inventory dollars")
    ap.add_argument("--max-inventory", type=float, default=None, help="max unpaired contracts per market (default = size)")
    ap.add_argument("--max-loss", type=float, default=50, help="stop if account value drops this many dollars")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--place", action="store_true", help="send real orders (default: dry run)")
    a = ap.parse_args()
    if STOP_FILE.exists():
        raise SystemExit(f"Kill switch is on: delete {STOP_FILE} to run.")
    auth, public = AuthKalshi.from_env(), Kalshi()
    books = pick_markets(public, a.markets, a.size, a.max_capital)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = DATA / "live" / run_id
    bot = Bot(auth, public, books, a.max_capital, a.max_inventory or a.size, a.max_loss, a.place, out)
    (out / "config.json").write_text(json.dumps({**vars(a), "markets_list": [b.ticker for b in books]}, indent=2))
    mode = "LIVE ORDERS" if a.place else "DRY RUN (nothing sent)"
    print(f"{mode}: {len(books)} markets, {a.size:g}/side, cap ${a.max_capital:g}, max loss ${a.max_loss:g} -> {out}",
          flush=True)
    if not a.place:  # dry run: one cycle, print what it would do
        bot.cycle()
        for line in (out / "actions.jsonl").read_text().splitlines():
            d = json.loads(line)
            if d.get("action") == "place":
                print(f"  would place {d['side']:3} {d['ticker']:40} {d['size']:>6g} @ {d['price']:.2f}")
            elif "skip" in d:
                print(f"  skip {d['ticker']} {d['side']} ({d['skip']})")
        return
    bot.run(a.hours, a.interval)


if __name__ == "__main__":
    main()
