import pytest

from kalshi_maker.paper import Book, process_trades, requote


def mk():
    return Book(ticker="T", daily=100, target=1000, df=0.5, fee_rate=0.0, size=100)


def trade(taker_side, yes_px, n, ts="2026-11-03T00:00:01Z"):
    return {"taker_side": taker_side, "yes_price_dollars": str(yes_px), "no_price_dollars": str(round(1 - yes_px, 2)),
            "count_fp": str(n), "created_time": ts}


def test_requote_joins_back_of_reference_level():
    b = mk()
    requote(b, "yes", [(0.40, 300), (0.39, 900)], 100)
    assert b.yes.price == 0.40 and b.yes.queue_ahead == 300 and b.yes.size == 100


def test_trade_at_our_price_eats_queue_first():
    b = mk()
    requote(b, "yes", [(0.40, 300), (0.39, 900)], 100)
    process_trades(b, [trade("no", 0.40, 250)], lambda d: None)
    assert b.pos_yes == 0 and b.yes.queue_ahead == 50
    process_trades(b, [trade("no", 0.40, 80, "2026-11-03T00:00:02Z")], lambda d: None)
    assert b.pos_yes == 30


def test_trade_through_fills_us_and_pairing_locks_profit():
    b = mk()
    requote(b, "yes", [(0.40, 300)], 100)
    requote(b, "no", [(0.55, 300)], 100)
    process_trades(b, [trade("no", 0.38, 500)], lambda d: None)  # swept through 0.40
    assert b.pos_yes == 100
    process_trades(b, [trade("yes", 0.44, 500, "2026-11-03T00:00:02Z")], lambda d: None)  # no at 0.56 > our 0.55? no
    assert b.pos_no == 0
    process_trades(b, [trade("yes", 0.46, 500, "2026-11-03T00:00:03Z")], lambda d: None)  # no at 0.54 < 0.55
    assert b.pos_yes == 0 and b.pos_no == 0
    assert b.realized == pytest.approx(100 * (1 - 0.40 - 0.55))


def test_inventory_cap_stops_requoting_long_side():
    b = mk()
    b.pos_yes = 100
    requote(b, "yes", [(0.40, 300)], 100)
    assert b.yes.price is None and b.yes.size == 0


def test_stops_quoting_near_resolved_price():
    b = mk()
    requote(b, "yes", [(0.97, 300)], 100)
    assert b.yes.price is None


def test_never_bids_a_pair_above_one_dollar():
    b = mk()
    b.pos_no, b.cost_no = 50, 50 * 0.30
    requote(b, "yes", [(0.72, 300)], 100)
    assert b.yes.price is None
    requote(b, "yes", [(0.65, 300)], 100)
    assert b.yes.price == 0.65


def test_risky_skips_events_already_dated_past():
    from datetime import date

    from kalshi_maker.paper import risky
    today = date(2026, 11, 4)
    assert risky("KXCABALLOTMEASURES-26NOV03-PR04", today)
    assert risky("KXOHACTONBROWN-26NOV03-R0", today)
    assert not risky("KXOHACTONBROWN-26DEC07-R0", today)
    assert not risky("KXEOWEEK-26NOV07-2", today)


def test_period_days():
    from kalshi_maker.paper import period_days
    assert period_days({"start_date": "2026-11-05T05:00:00Z", "end_date": "2026-11-06T05:00:00Z"}) == 1


def test_event_of_groups_strikes_of_one_event():
    from kalshi_maker.paper import event_of
    assert event_of("KXRT-CAT-55") == event_of("KXRT-CAT-50") == "KXRT-CAT"
    assert event_of("KXEOWEEK-26NOV07-1") == "KXEOWEEK-26NOV07"
