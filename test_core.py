from datetime import datetime
import unittest

from core import (
    Event, EventType, OptionEvent,
    calculate_gain_for_ticker, calculate_option_gain_for_contract, get_deemed_disposals,
)

class CalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default_start_date = datetime(2025, 1, 1, 0, 0, 0)
        cls.default_end_date = datetime(2025, 12, 31, 23, 59, 59)

    def test_gain_within_date_range(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 1, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 3, 1, 0, 0, 0), num_shares=50, value=12.0),
            Event(evType=EventType.SELL, date=datetime(2025, 7, 1, 0, 0, 0), num_shares=50, value=13.0),
        ]
        # Only consider sales between March and July
        start_date = datetime(2025, 3, 1, 0, 0, 0)
        end_date = datetime(2025, 7, 1, 0, 0, 0)
        expected_output = 100 + 150
        result = calculate_gain_for_ticker(test_input, start_date, end_date)
        self.assertEqual(result, expected_output)

    def test_gain_outside_date_range(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 1, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 2, 1, 0, 0, 0), num_shares=50, value=12.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 1, 0, 0, 0), num_shares=50, value=13.0),
        ]
        # Only consider sales between March and July (should be zero)
        start_date = datetime(2025, 3, 1, 0, 0, 0)
        end_date = datetime(2025, 7, 1, 0, 0, 0)
        expected_output = 0
        result = calculate_gain_for_ticker(test_input, start_date, end_date)
        self.assertEqual(result, expected_output)

    def test_gain_partial_date_range(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 1, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 4, 1, 0, 0, 0), num_shares=50, value=12.0),
            Event(evType=EventType.SELL, date=datetime(2025, 10, 1, 0, 0, 0), num_shares=50, value=13.0),
        ]
        # Only consider sales between March and September
        start_date = datetime(2025, 3, 1, 0, 0, 0)
        end_date = datetime(2025, 9, 30, 23, 59, 59)
        expected_output = 100  # Only the April sale
        result = calculate_gain_for_ticker(test_input, start_date, end_date)
        self.assertEqual(result, expected_output)

    def test_buy_sell_same_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=12.0)
        ]
        expected_output = 200
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_buy_sell_same_year_loss(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=8.0)
        ]
        expected_output = -200
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_buy_partial_sell_same_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=50, value=12.0)
        ]
        expected_output = 100
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_loss(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=8.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = -400
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_loss2(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=11.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = -100
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_gain(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=13.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = 100
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_big_gain(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=11.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=11.0),
        ]
        expected_output = 300
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 30, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=13.0),
        ]
        expected_output = 300
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_buy_more_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=150, value=11.0),
        ]
        expected_output = 200
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_partial_sell_last_year_buy_more_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2024, 6, 15, 0, 0, 0, 0), num_shares=50, value=13.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=150, value=11.0),
        ]
        expected_output = 250
        result = calculate_gain_for_ticker(test_input, self.default_start_date, self.default_end_date)
        self.assertEqual(result, expected_output)


class SameDayRuleTests(unittest.TestCase):
    """same-day acquisitions matched before FIFO pool."""

    def setUp(self):
        self.start = datetime(2025, 1, 1)
        self.end = datetime(2025, 12, 31, 23, 59, 59)

    def test_sell_matched_against_same_day_buy_not_fifo(self):
        # Without same-day rule: sell @ 18 vs FIFO buy @ 10 → gain 800
        # With same-day rule:    sell @ 18 vs same-day buy @ 15 → gain 300
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1), 100, 10.0),
            Event(EventType.BUY,  datetime(2025, 6, 1), 100, 15.0),
            Event(EventType.SELL, datetime(2025, 6, 1), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 300.0)

    def test_same_day_buy_partially_covers_sell(self):
        # Same-day buy covers 60 of 100 shares; remaining 40 come from FIFO pool
        # Same-day:  (18-15)*60 = 180
        # FIFO:      (18-10)*40 = 320
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1), 100, 10.0),
            Event(EventType.BUY,  datetime(2025, 6, 1),  60, 15.0),
            Event(EventType.SELL, datetime(2025, 6, 1), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 500.0)

    def test_no_same_day_buy_falls_back_to_fifo(self):
        # No same-day buy → normal FIFO: (18-10)*100 = 800
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1), 100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 800.0)


class FourWeekBackwardRuleTests(unittest.TestCase):
    """(Revenue 6A.3.1): acquisitions within 4 weeks before disposal
    are matched before the FIFO historical pool."""

    def setUp(self):
        self.start = datetime(2025, 1, 1)
        self.end = datetime(2025, 12, 31, 23, 59, 59)

    def test_recent_acquisition_matched_before_fifo(self):
        # Old FIFO lot at 10, recent buy 14 days before sell at 15
        # Backward rule: sell @ 18 vs recent buy @ 15 → gain 300
        # FIFO would give: sell @ 18 vs old buy @ 10 → gain 800
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.BUY,  datetime(2025, 6, 1),  100, 15.0),   # 14 days before sell
            Event(EventType.SELL, datetime(2025, 6, 15), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 300.0)

    def test_acquisition_exactly_28_days_before_is_in_window(self):
        # Jun 15 − 28 days = May 18 → May 18 is the cutoff (inclusive)
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.BUY,  datetime(2025, 5, 18), 100, 15.0),   # exactly 28 days before
            Event(EventType.SELL, datetime(2025, 6, 15), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 300.0)

    def test_acquisition_29_days_before_falls_into_fifo(self):
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.BUY,  datetime(2025, 5, 17), 100, 15.0),   # 29 days before → FIFO
            Event(EventType.SELL, datetime(2025, 6, 15), 100, 18.0),
        ]
        # Both lots in FIFO (oldest first) → matched against Jan 1 @ 10 → gain 800
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 800.0)

    def test_backward_rule_partially_covers_sell(self):
        # Recent buy covers 60 shares; remaining 40 from FIFO
        # Backward: (18-15)*60 = 180
        # FIFO:     (18-10)*40 = 320
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.BUY,  datetime(2025, 6, 1),   60, 15.0),   # within 28 days
            Event(EventType.SELL, datetime(2025, 6, 15), 100, 18.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 500.0)

    def test_backward_rule_takes_priority_over_forward_bb(self):
        # Backward buy (7 days before) should be matched before forward B&B buy (7 days after)
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.BUY,  datetime(2025, 6, 8),  100, 15.0),   # 7 days before sell
            Event(EventType.SELL, datetime(2025, 6, 15), 100, 18.0),
            Event(EventType.BUY,  datetime(2025, 6, 22), 100,  9.0),   # 7 days after sell (B&B)
        ]
        # P2 backward: (18-15)*100 = 300 (not the B&B @ 9)
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 300.0)


class BedAndBreakfastRuleTests(unittest.TestCase):
    """acquisitions within 28 days following disposal matched before FIFO."""

    def setUp(self):
        self.start = datetime(2025, 1, 1)
        self.end = datetime(2025, 12, 31, 23, 59, 59)

    def test_loss_deferred_when_rebuy_within_28_days(self):
        # Without B&B: sell @ 8 vs buy @ 10 → loss -200
        # With B&B:    sell @ 8 vs rebuy @ 8.5 → loss -50 (rest deferred)
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1),  100,  8.0),
            Event(EventType.BUY,  datetime(2025, 6, 20), 100,  8.5),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), -50.0)

    def test_exactly_28_days_still_within_window(self):
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1),  100,  8.0),
            Event(EventType.BUY,  datetime(2025, 6, 29), 100,  8.5),  # exactly 28 days later
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), -50.0)

    def test_29_days_falls_outside_window_uses_fifo(self):
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1),  100,  8.0),
            Event(EventType.BUY,  datetime(2025, 6, 30), 100,  8.5),  # 29 days later
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), -200.0)

    def test_gain_also_matched_against_28_day_rebuy(self):
        # Rule applies to gains too, not just losses
        # sell @ 12, rebuy @ 9 within 28 days → gain = (12-9)*100 = 300
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1),  100, 12.0),
            Event(EventType.BUY,  datetime(2025, 6, 15), 100,  9.0),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), 300.0)

    def test_rebuy_only_partially_covers_sell(self):
        # Sell 100, rebuy 60 within 28 days, FIFO covers remaining 40
        # B&B:  (8-8.5)*60 = -30
        # FIFO: (8-10)*40  = -80
        events = [
            Event(EventType.BUY,  datetime(2025, 1, 1),  100, 10.0),
            Event(EventType.SELL, datetime(2025, 6, 1),  100,  8.0),
            Event(EventType.BUY,  datetime(2025, 6, 15),  60,  8.5),
        ]
        self.assertEqual(calculate_gain_for_ticker(events, self.start, self.end), -110.0)


class DeemedDisposalTests(unittest.TestCase):
    """Tests for get_deemed_disposals: 8-year exit tax rule for Irish-domiciled funds."""

    def setUp(self):
        self.start_2025 = datetime(2025, 1, 1)
        self.end_2025 = datetime(2025, 12, 31, 23, 59, 59)

    def test_anniversary_within_period_triggers(self):
        events = [Event(EventType.BUY, datetime(2017, 3, 15), 100, 10.0)]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], datetime(2025, 3, 15))
        self.assertAlmostEqual(result[0][1], 100.0)
        self.assertEqual(result[0][2], 10.0)

    def test_anniversary_outside_period_does_not_trigger(self):
        events = [Event(EventType.BUY, datetime(2020, 6, 1), 100, 10.0)]
        # Anniversary: 2028-06-01 — not within 2025
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 0)

    def test_anniversary_on_period_boundary_triggers(self):
        events = [Event(EventType.BUY, datetime(2017, 1, 1), 100, 10.0)]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], datetime(2025, 1, 1))

    def test_prior_sells_reduce_shares_subject_to_deemed_disposal(self):
        events = [
            Event(EventType.BUY,  datetime(2017, 1, 1), 100, 10.0),
            Event(EventType.SELL, datetime(2020, 6, 1),  40, 12.0),  # sold 40 before anniversary
        ]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][1], 60.0)

    def test_fully_sold_lot_does_not_trigger(self):
        events = [
            Event(EventType.BUY,  datetime(2017, 1, 1), 100, 10.0),
            Event(EventType.SELL, datetime(2020, 6, 1), 100, 12.0),  # fully sold
        ]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 0)

    def test_multiple_lots_only_matching_anniversary_triggers(self):
        events = [
            Event(EventType.BUY, datetime(2017, 3, 1),  50, 10.0),  # anniversary 2025-03-01 ✓
            Event(EventType.BUY, datetime(2018, 9, 1),  50, 12.0),  # anniversary 2026-09-01 ✗
        ]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][1], 50.0)
        self.assertEqual(result[0][2], 10.0)

    def test_multiple_lots_same_period_both_trigger(self):
        events = [
            Event(EventType.BUY, datetime(2017, 2, 1), 50, 10.0),  # anniversary 2025-02-01
            Event(EventType.BUY, datetime(2017, 8, 1), 75, 11.0),  # anniversary 2025-08-01
        ]
        result = get_deemed_disposals(events, self.start_2025, self.end_2025)
        self.assertEqual(len(result), 2)
        # Results sorted by anniversary date
        self.assertEqual(result[0][0], datetime(2025, 2, 1))
        self.assertEqual(result[1][0], datetime(2025, 8, 1))


class OptionGainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start_2025 = datetime(2025, 1, 1)
        cls.end_2025 = datetime(2025, 12, 31, 23, 59, 59)

    def test_bought_option_closed_for_gain(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=True, quantity=1, net_cash=-150.0),
            OptionEvent(datetime(2025, 2, 1), is_open=False, quantity=1, net_cash=190.0),
        ]
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertAlmostEqual(result, 40.0)

    def test_written_option_closed_for_loss(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=True, quantity=1, net_cash=150.0),
            OptionEvent(datetime(2025, 2, 1), is_open=False, quantity=1, net_cash=-200.0),
        ]
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertAlmostEqual(result, -50.0)

    def test_bought_option_expires_worthless_is_full_loss(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=True, quantity=1, net_cash=-150.0),
            OptionEvent(datetime(2025, 3, 1), is_open=False, quantity=1, net_cash=0.0),
        ]
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertAlmostEqual(result, -150.0)

    def test_written_option_expires_worthless_is_full_gain(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=True, quantity=1, net_cash=150.0),
            OptionEvent(datetime(2025, 3, 1), is_open=False, quantity=1, net_cash=0.0),
        ]
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertAlmostEqual(result, 150.0)

    def test_multi_lot_fifo_partial_fills(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=True, quantity=2, net_cash=-200.0),  # -100/contract
            OptionEvent(datetime(2025, 1, 5), is_open=True, quantity=1, net_cash=-150.0),  # -150/contract
            OptionEvent(datetime(2025, 2, 1), is_open=False, quantity=3, net_cash=450.0),  # +150/contract
        ]
        # FIFO: 2 contracts @ -100 + 1 contract @ -150 = -350 cost, closed for +450 => gain 100
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertAlmostEqual(result, 100.0)

    def test_close_outside_date_range_excluded(self):
        events = [
            OptionEvent(datetime(2024, 6, 1), is_open=True, quantity=1, net_cash=-150.0),
            OptionEvent(datetime(2024, 7, 1), is_open=False, quantity=1, net_cash=190.0),
        ]
        result = calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)
        self.assertEqual(result, 0.0)

    def test_close_without_matching_open_raises(self):
        events = [
            OptionEvent(datetime(2025, 1, 1), is_open=False, quantity=1, net_cash=100.0),
        ]
        with self.assertRaises(ValueError):
            calculate_option_gain_for_contract(events, self.start_2025, self.end_2025)


if __name__ == '__main__':
    unittest.main()
