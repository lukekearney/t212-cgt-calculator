"""IBKR-specific tests (Flex Query XML parsing, DB import)."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core import EventType
from ibkr import (
    parse_flex_report, read_flex, calculate_income_from_flex, import_flex_to_db,
    parse_activity_csv, read_activity_csv, read_option_events_from_csv,
    calculate_income_from_activity_csv, import_activity_csv_to_db,
)

_SAMPLE_FLEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement accountId="U1234567" fromDate="20250101" toDate="20251231">
      <Trades>
        <Trade transactionID="T1" tradeID="T1" symbol="AAPL" description="APPLE INC"
               isin="US0378331005" currency="USD" fxRateToBase="0.9"
               dateTime="20250101;103000" quantity="10" tradePrice="100"
               ibCommission="-1" buySell="BUY" assetCategory="STK"/>
        <Trade transactionID="T2" tradeID="T2" symbol="AAPL" description="APPLE INC"
               isin="US0378331005" currency="USD" fxRateToBase="0.9"
               dateTime="20250601;103000" quantity="10" tradePrice="150"
               ibCommission="-1" buySell="SELL" assetCategory="STK"/>
      </Trades>
      <CashTransactions>
        <CashTransaction transactionID="C1" type="Dividends" symbol="AAPL"
                          description="APPLE INC" isin="US0378331005" currency="USD"
                          fxRateToBase="0.9" amount="10" dateTime="20250301"/>
        <CashTransaction transactionID="C2" type="Withholding Tax" symbol="AAPL"
                          description="APPLE INC" isin="US0378331005" currency="USD"
                          fxRateToBase="0.9" amount="-1.5" dateTime="20250301"/>
        <CashTransaction transactionID="C3" type="Broker Interest Received"
                          currency="USD" fxRateToBase="0.9" amount="5" dateTime="20250215"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


_SAMPLE_ACTIVITY_CSV = """ClientAccountID,Symbol,Description,AssetClass,Buy/Sell,Quantity,TradeDate,TradePrice,TradeMoney,Proceeds,CostBasis,IBCommission,Taxes,NetCash,CurrencyPrimary,FXRateToBase,FifoPnlRealized,Open/CloseIndicator,SettleDateTarget,OpenDateTime,HoldingPeriodDateTime,ISIN,TransactionType
U1,AAPL,APPLE INC,STK,BUY,10,20250101,100,1000,-1000,1000,-1,0,-1001,USD,0.9,0,O,20250103,,,US0378331005,ExchTrade
U1,AAPL,APPLE INC,STK,SELL,-10,20250601,150,-1500,1500,-1000,-1,0,1499,USD,0.9,500,C,20250603,,,US0378331005,ExchTrade
U1,AAPL,APPLE INC,STK,SELL,10,20250601,150,,,1000,,,,USD,0.9,500,C,,20250101;000000,20250101;000000,US0378331005,
U1,TEST  250601C00100000,TEST 01JUN25 100 C,OPT,BUY,1,20250101,1.5,150,-150,151,-1,0,-151,USD,0.9,0,O,20250103,,,,ExchTrade
U1,TEST  250601C00100000,TEST 01JUN25 100 C,OPT,SELL,-1,20250201,2.0,-200,200,-151,-1,0,198,USD,0.9,47,C,20250203,,,,ExchTrade
U1,TEST  250601C00100000,TEST 01JUN25 100 C,OPT,SELL,1,20250201,2.0,,,151,,,,USD,0.9,47,C,,20250101;000000,20250101;000000,,
U1,EXPD  250301C00050000,EXPD 01MAR25 50 C,OPT,BUY,1,20250115,0.9,90,-90,91,-1,0,-91,USD,0.9,0,O,20250117,,,,ExchTrade
U1,EXPD  250301C00050000,EXPD 01MAR25 50 C,OPT,SELL,-1,20250301,0,0,0,-90,0,0,0,USD,0.9,-90,C,20250304,,,,BookTrade
ClientAccountID,Date/Time,Type,Symbol,Description,Amount,CurrencyPrimary,FXRateToBase,AssetClass,ISIN,ExDate,DividendType
U1,20250301,Dividends,AAPL,APPLE INC,10,USD,0.9,STK,US0378331005,,
U1,20250301,Withholding Tax,AAPL,APPLE INC,-1.5,USD,0.9,STK,US0378331005,,
U1,20250215,Broker Interest Received,,,5,USD,0.9,CASH,,,
"""


class IbkrActivityCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmpdir.name) / "activity.csv"
        self.csv_path.write_text(_SAMPLE_ACTIVITY_CSV, encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_parse_activity_csv_splits_sections_and_skips_blank_type_rows(self):
        trades, cash_txs = parse_activity_csv(self.csv_path)
        # Only ExchTrade/BookTrade rows should be present; the file has
        # 8 trade rows total, 2 of which are blank-TransactionType detail rows.
        real_trades = [t for t in trades if t["TransactionType"] in ("ExchTrade", "BookTrade")]
        self.assertEqual(len(real_trades), 6)
        self.assertEqual(len(trades), 8)
        self.assertEqual(len(cash_txs), 3)

    def test_read_activity_csv_parses_stock_trades(self):
        events = read_activity_csv(self.csv_path, "EUR")
        evs = events["AAPL(APPLE INC)"]
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].evType, EventType.BUY)
        self.assertAlmostEqual(evs[0].value, 90.0)
        self.assertEqual(evs[1].evType, EventType.SELL)
        self.assertAlmostEqual(evs[1].value, 135.0)

    def test_read_option_events_ignores_blank_detail_rows(self):
        events = read_option_events_from_csv(self.csv_path, "EUR")
        test_key = "TEST  250601C00100000(TEST 01JUN25 100 C)"
        self.assertEqual(len(events[test_key]), 2)
        opens = [e for e in events[test_key] if e.is_open]
        closes = [e for e in events[test_key] if not e.is_open]
        self.assertAlmostEqual(opens[0].net_cash, -135.9)
        self.assertAlmostEqual(closes[0].net_cash, 178.2)

        expd_key = "EXPD  250301C00050000(EXPD 01MAR25 50 C)"
        expd_close = [e for e in events[expd_key] if not e.is_open][0]
        self.assertAlmostEqual(expd_close.net_cash, 0.0)

    def test_calculate_income_from_activity_csv(self):
        start = datetime(2025, 1, 1)
        end = datetime(2025, 12, 31, 23, 59, 59)
        total_interest, dividends = calculate_income_from_activity_csv(self.csv_path, "EUR", start, end)
        self.assertAlmostEqual(total_interest, 4.5)
        div = dividends["AAPL(APPLE INC)"]
        self.assertAlmostEqual(div.gross_eur, 9.0)
        self.assertAlmostEqual(div.withholding_eur, 1.35)

    def test_import_activity_csv_to_db(self):
        db_path = os.path.join(self.tmpdir.name, "test.db")
        trades_added, interest_added, dividends_added, options_added = import_activity_csv_to_db(
            self.csv_path, db_path, "EUR"
        )
        self.assertEqual(trades_added, 2)
        self.assertEqual(interest_added, 1)
        self.assertEqual(dividends_added, 2)
        self.assertEqual(options_added, 4)

        conn = sqlite3.connect(db_path)
        option_brokers = [r[0] for r in conn.execute("SELECT broker FROM option_trades").fetchall()]
        conn.close()
        self.assertTrue(all(b == "IBKR" for b in option_brokers))


class IbkrFlexTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.xml_path = Path(self.tmpdir.name) / "flex.xml"
        self.xml_path.write_text(_SAMPLE_FLEX_XML, encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_parse_flex_report_counts_elements(self):
        trades, cash_txs = parse_flex_report(self.xml_path)
        self.assertEqual(len(trades), 2)
        self.assertEqual(len(cash_txs), 3)

    def test_read_flex_applies_fx_and_direction(self):
        events = read_flex(self.xml_path, "EUR")
        evs = events["AAPL(APPLE INC)"]
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].evType, EventType.BUY)
        self.assertAlmostEqual(evs[0].value, 90.0)
        self.assertEqual(evs[1].evType, EventType.SELL)
        self.assertAlmostEqual(evs[1].value, 135.0)
        self.assertAlmostEqual(evs[0].cost_of_transaction, 0.9)

    def test_calculate_income_from_flex(self):
        start = datetime(2025, 1, 1)
        end = datetime(2025, 12, 31, 23, 59, 59)
        total_interest, dividends = calculate_income_from_flex(self.xml_path, "EUR", start, end)
        self.assertAlmostEqual(total_interest, 4.5)
        div = dividends["AAPL(APPLE INC)"]
        self.assertTrue(div.is_us)
        self.assertAlmostEqual(div.gross_eur, 9.0)
        self.assertAlmostEqual(div.withholding_eur, 1.35)

    def test_import_flex_to_db_tags_broker_ibkr(self):
        db_path = os.path.join(self.tmpdir.name, "test.db")
        trades_added, interest_added, dividends_added = import_flex_to_db(self.xml_path, db_path, "EUR")
        self.assertEqual(trades_added, 2)
        self.assertEqual(interest_added, 1)
        self.assertEqual(dividends_added, 2)

        conn = sqlite3.connect(db_path)
        brokers = [r[0] for r in conn.execute("SELECT broker FROM trades").fetchall()]
        conn.close()
        self.assertTrue(all(b == "IBKR" for b in brokers))


if __name__ == '__main__':
    unittest.main()
