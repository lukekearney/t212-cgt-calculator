from datetime import datetime
import unittest

from t212 import Event, EventType, calculate_gain_for_ticker

class CalculationTests(unittest.TestCase):
    def test_buy_sell_same_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=12.0)
        ]
        expected_output = 200
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_buy_sell_same_year_loss(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=8.0)
        ]
        expected_output = -200
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_buy_partial_sell_same_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=50, value=12.0)
        ]
        expected_output = 100
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_loss(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=8.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = -400
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_loss2(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=11.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = -100
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_gain(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=13.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=7.0),
        ]
        expected_output = 100
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_multi_buy_sell_buy_sell_same_year_big_gain(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2025, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=11.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=100, value=11.0),
        ]
        expected_output = 300
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 30, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2025, 6, 15, 0, 0, 0, 0), num_shares=100, value=13.0),
        ]
        expected_output = 300
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_buy_more_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=150, value=11.0),
        ]
        expected_output = 200
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)

    def test_buy_prev_year_partial_sell_last_year_buy_more_sell_this_year(self):
        test_input = [
            Event(evType=EventType.BUY, date=datetime(2024, 1, 31, 0, 0, 0, 0), num_shares=100, value=10.0),
            Event(evType=EventType.SELL, date=datetime(2024, 6, 15, 0, 0, 0, 0), num_shares=50, value=13.0),
            Event(evType=EventType.BUY, date=datetime(2025, 7, 1, 0, 0, 0, 0), num_shares=100, value=9.0),
            Event(evType=EventType.SELL, date=datetime(2025, 8, 20, 0, 0, 0, 0), num_shares=150, value=11.0),
        ]
        expected_output = 250
        result = calculate_gain_for_ticker(test_input)
        self.assertEqual(result, expected_output)