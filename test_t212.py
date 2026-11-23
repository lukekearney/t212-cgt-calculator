"""T212-specific integration tests (CSV parsing, DB import, API client)."""
import csv
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core import EventType
from t212 import read_csv, calculate_income_from_csv, import_csv_to_db

_HEADER = [
    "Action", "Time", "ID", "Ticker", "Name", "No. of shares", "Price / share",
    "Currency (Price / share)", "Exchange rate", "Stamp duty reserve tax",
    "Currency conversion fee", "French transaction tax", "Finra fee",
    "Total", "ISIN", "Withholding tax",
]


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_HEADER)
        writer.writeheader()
        for row in rows:
            full = {h: "" for h in _HEADER}
            full.update(row)
            writer.writerow(full)


class T212CsvTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmpdir.name) / "export.csv"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_read_csv_parses_buy_and_sell(self):
        _write_csv(self.csv_path, [
            {"Action": "Market buy", "Time": "2025-01-01 10:00:00", "Ticker": "AAPL", "Name": "Apple",
             "No. of shares": "10", "Price / share": "100", "Currency (Price / share)": "EUR", "Exchange rate": "1"},
            {"Action": "Market sell", "Time": "2025-06-01 10:00:00", "Ticker": "AAPL", "Name": "Apple",
             "No. of shares": "10", "Price / share": "150", "Currency (Price / share)": "EUR", "Exchange rate": "1"},
        ])
        events = read_csv(self.csv_path, "EUR")
        self.assertIn("AAPL(Apple)", events)
        evs = events["AAPL(Apple)"]
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].evType, EventType.BUY)
        self.assertEqual(evs[1].evType, EventType.SELL)
        self.assertEqual(evs[1].value, 150.0)

    def test_read_csv_applies_fx_conversion(self):
        _write_csv(self.csv_path, [
            {"Action": "Market buy", "Time": "2025-01-01 10:00:00", "Ticker": "AAPL", "Name": "Apple",
             "No. of shares": "10", "Price / share": "100", "Currency (Price / share)": "USD", "Exchange rate": "0.9"},
        ])
        events = read_csv(self.csv_path, "EUR")
        self.assertAlmostEqual(events["AAPL(Apple)"][0].value, 90.0)

    def test_calculate_income_from_csv_interest_and_dividends(self):
        _write_csv(self.csv_path, [
            {"Action": "Interest on cash", "Time": "2025-02-01 00:00:00", "Total": "5.0"},
            {"Action": "Dividend (Ordinary)", "Time": "2025-03-01 00:00:00", "Ticker": "AAPL", "Name": "Apple",
             "ISIN": "US0378331005", "Total": "10.0", "Withholding tax": "1.5", "Exchange rate": "1"},
        ])
        start = datetime(2025, 1, 1)
        end = datetime(2025, 12, 31, 23, 59, 59)
        total_interest, dividends = calculate_income_from_csv(self.csv_path, "EUR", start, end)
        self.assertEqual(total_interest, 5.0)
        div = dividends["AAPL(Apple)"]
        self.assertTrue(div.is_us)
        self.assertEqual(div.gross_eur, 10.0)
        self.assertEqual(div.withholding_eur, 1.5)

    def test_import_csv_to_db_tags_broker_t212(self):
        _write_csv(self.csv_path, [
            {"Action": "Market buy", "Time": "2025-01-01 10:00:00", "ID": "1", "Ticker": "AAPL", "Name": "Apple",
             "No. of shares": "10", "Price / share": "100", "Currency (Price / share)": "EUR", "Exchange rate": "1"},
        ])
        db_path = os.path.join(self.tmpdir.name, "test.db")
        trades_added, interest_added, dividends_added = import_csv_to_db(self.csv_path, db_path, "EUR")
        self.assertEqual((trades_added, interest_added, dividends_added), (1, 0, 0))

        conn = sqlite3.connect(db_path)
        broker = conn.execute("SELECT broker FROM trades WHERE id = '1'").fetchone()[0]
        conn.close()
        self.assertEqual(broker, "T212")


if __name__ == '__main__':
    unittest.main()
