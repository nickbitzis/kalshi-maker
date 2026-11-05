import itertools

from kalshi_maker.bot import Bot, apply_position, my_orders, plan, remove_mine
from kalshi_maker.paper import Book


class FakeExchange:
    """Stands in for both the signed client and the public one."""

    def __init__(self, book, value=100_000):
        self.book, self.value = book, value
        self.orders: dict[str, dict] = {}
        self.positions: list[dict] = []
        self.sent: list[tuple] = []
        self.ids = itertools.count(1)
        self.open = True

    # public
    def trading_active(self):
        return self.open

    def orderbooks(self, tickers):
        # our own resting orders show up in the public book, like on the real exchange
        out = {}
        for t in tickers:
            yes, no = dict(self.book["yes"]), dict(self.book["no"])
            for o in self.orders.values():
                if o["ticker"] != t:
                    continue
                px, n = float(o["yes_price_dollars"]), float(o["remaining_count_fp"])
                if o["book_side"] == "bid":
                    yes[px] = yes.get(px, 0) + n
                else:
                    no[round(1 - px, 4)] = no.get(round(1 - px, 4), 0) + n
            out[t] = {"yes": sorted(yes.items()), "no": sorted(no.items())}
        return out

    # signed
    def balance_cents(self):
        return self.value

    def resting_orders(self):
        return list(self.orders.values())

    def market_positions(self):
        return self.positions

    def create_order(self, ticker, side, yes_price, count):
        oid = f"o{next(self.ids)}"
        self.orders[oid] = {"order_id": oid, "ticker": ticker, "book_side": side,
                            "yes_price_dollars": f"{yes_price:.4f}", "remaining_count_fp": f"{count:.2f}"}
        self.sent.append(("place", ticker, side, yes_price, count))
        return {"order_id": oid}

    def cancel(self, oid, ticker):
        self.orders.pop(oid)
        self.sent.append(("cancel", oid))

    def cancel_all(self):
        self.orders.clear()
        self.sent.append(("cancel_all",))


def mk(size=100):
    return Book(ticker="T", daily=100, target=1000, df=0.5, fee_rate=0.0, size=size)


BOOK = {"yes": [(0.39, 900), (0.40, 300)], "no": [(0.54, 900), (0.55, 300)]}


def bot(ex, tmp_path, **kw):
    args = dict(max_capital=1000, max_inventory=100, max_loss=50, place=True)
    args.update(kw)
    return Bot(ex, ex, [mk()], out_dir=tmp_path / "run", stop_file=tmp_path / "STOP", **args)


def test_remove_mine_takes_our_size_out_of_the_level():
    assert remove_mine([(0.40, 400), (0.39, 900)], 0.40, 100) == [(0.40, 300), (0.39, 900)]
    assert remove_mine([(0.40, 100)], 0.40, 100) == []


def test_no_bid_is_sent_as_sell_yes_at_one_minus_price(tmp_path):
    ex = FakeExchange(BOOK)
    bot(ex, tmp_path).cycle()
    assert ("place", "T", "bid", 0.40, 100) in ex.sent
    assert ("place", "T", "ask", 0.45, 100) in ex.sent  # no bid 0.55 == sell yes 0.45


def test_second_cycle_keeps_orders_instead_of_chasing_itself(tmp_path):
    ex = FakeExchange(BOOK)
    b = bot(ex, tmp_path)
    b.cycle()
    n = len(ex.sent)
    b.cycle()
    assert len(ex.sent) == n


def test_moves_order_when_reference_price_moves(tmp_path):
    ex = FakeExchange(BOOK)
    b = bot(ex, tmp_path)
    b.cycle()
    ex.book = {"yes": [(0.41, 300), (0.42, 900)], "no": BOOK["no"]}
    b.cycle()
    kinds = [s[0] for s in ex.sent[2:]]
    assert kinds == ["cancel", "place"] and ex.sent[-1][3] == 0.42


def test_capital_cap_skips_orders_that_do_not_fit(tmp_path):
    ex = FakeExchange(BOOK)
    bot(ex, tmp_path, max_capital=50).cycle()  # yes side costs $40, no side $55
    assert [s[2] for s in ex.sent] == ["bid"]


def test_long_yes_inventory_pulls_yes_bid(tmp_path):
    ex = FakeExchange(BOOK)
    ex.positions = [{"ticker": "T", "position_fp": "100.00", "market_exposure_dollars": "40.00"}]
    bot(ex, tmp_path).cycle()
    assert [s[2] for s in ex.sent] == ["ask"]


def test_dry_run_sends_nothing(tmp_path):
    ex = FakeExchange(BOOK)
    bot(ex, tmp_path, place=False).cycle()
    assert ex.sent == []


def test_max_loss_and_kill_switch_stop_and_cancel_all(tmp_path):
    ex = FakeExchange(BOOK)
    b = bot(ex, tmp_path)
    b.cycle()
    ex.value -= 6000  # down $60 > $50
    assert b.cycle().startswith("max loss")
    ex.value += 6000
    (tmp_path / "STOP").touch()
    assert b.cycle().startswith("kill switch")
    b.shutdown("test")
    assert ex.orders == {} and ex.sent[-1] == ("cancel_all",)


def test_run_cancels_everything_on_exit(tmp_path):
    ex = FakeExchange(BOOK)
    bot(ex, tmp_path).run(hours=0.0005, interval=0.5)
    assert ex.orders == {} and ex.sent[-1] == ("cancel_all",)


def test_apply_position_negative_is_no():
    b = mk()
    apply_position(b, {"position_fp": "-30.00", "market_exposure_dollars": "16.50"})
    assert b.pos_no == 30 and b.cost_no == 16.5 and b.pos_yes == 0


def test_my_orders_converts_ask_to_no_price():
    m = my_orders([{"order_id": "a", "ticker": "T", "book_side": "ask", "yes_price_dollars": "0.4500",
                    "remaining_count_fp": "10.00"}])
    assert m["T"]["no"][0]["price"] == 0.55


def test_plan_cancels_duplicate_orders_on_one_side():
    b = mk()
    mine = {"yes": [{"id": "a", "price": 0.40, "remaining": 100}, {"id": "b", "price": 0.40, "remaining": 100}]}
    acts = plan(b, {"yes": [(0.39, 900), (0.40, 500)], "no": BOOK["no"]}, mine, 100)
    assert ("cancel", "yes", "b") in acts and not any(a[0] == "place" and a[1] == "yes" for a in acts)


def test_waits_while_exchange_is_closed(tmp_path):
    ex = FakeExchange(BOOK)
    ex.open = False
    b = bot(ex, tmp_path)
    assert b.cycle() is None and ex.sent == []
    ex.open = True
    b.cycle()
    assert len(ex.sent) == 2
