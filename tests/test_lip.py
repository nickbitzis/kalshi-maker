import pytest

from kalshi_maker.lip import maker_fee, score_side, snapshot_share


def test_reference_price_walks_to_fifth_of_target():
    # target 1000 -> ref at first level with cum >= 200
    levels = [(0.50, 100), (0.49, 150), (0.48, 2000)]
    s = score_side(levels, 1000, 0.5)
    assert s.qualifies and s.ref_price == 0.49


def test_side_below_target_does_not_qualify():
    s = score_side([(0.5, 300), (0.4, 300)], 1000, 0.5)
    assert not s.qualifies and s.share == 0


def test_my_order_can_make_side_qualify_and_take_everything():
    s = score_side([(0.5, 300)], 1000, 0.5, mine=(0.5, 1000))
    assert s.qualifies and s.share == pytest.approx(1000 / 1300)


def test_discount_below_reference():
    # ref = 0.50 (500 >= 200). my order 2 ticks below -> 0.25x
    s = score_side([(0.50, 500), (0.48, 600)], 1000, 0.5, mine=(0.48, 400))
    assert s.ref_price == 0.50
    assert s.total_raw == pytest.approx(500 + (600 + 400) * 0.25)
    assert s.mine_raw == pytest.approx(100)


def test_orders_beyond_target_depth_do_not_count():
    s = score_side([(0.50, 1000), (0.40, 5000)], 1000, 0.5, mine=(0.40, 100))
    assert s.mine_raw == 0


def test_snapshot_share_is_average_of_sides_and_needs_both():
    book = {"yes": [(0.30, 1000)], "no": [(0.60, 3000)]}
    share, valid = snapshot_share(book, 1000, 0.5, my_yes=(0.30, 1000), my_no=(0.60, 1000))
    assert valid
    assert share == pytest.approx((0.5 + 0.25) / 2)
    share, valid = snapshot_share({"yes": [], "no": [(0.6, 3000)]}, 1000, 0.5)
    assert not valid and share == 0


def test_help_page_payout_example():
    # $100 reward, 8000/10000 valid snapshots, 20% share -> $16
    assert 100 * 0.20 * 0.8 == pytest.approx(16)


def test_fee():
    assert maker_fee(0.5, 100, 0.0175) == pytest.approx(0.4375)
