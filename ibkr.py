"""Interactive Brokers-specific parsing and import, via Flex Query XML reports.

Flex Query reports are configured in IBKR Account Management (Reporting > Flex
Queries) and downloaded manually, then imported with --import-flex or --flex.
The report must include a Trades section and a CashTransactions section, with
(at minimum) these fields enabled:

  Trades:           symbol, isin, currency, fxRateToBase, dateTime, quantity,
                     tradePrice, ibCommission, buySell, assetCategory
  CashTransactions: type, symbol, isin, currency, fxRateToBase, amount,
                     dateTime, description, transactionID

Field names/availability depend on the user's Flex Query template — if a
real export uses different attribute names, adjust the _attr() lookups below.
"""
import csv
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from core import Event, EventType, DividendInfo, OptionEvent, init_db

_INTEREST_TYPES = ("Broker Interest Received", "Broker Interest Paid")
_DIVIDEND_TYPES = ("Dividends", "Payment in Lieu of Dividends")


def _attr(elem: ET.Element, *names: str, default: str = "") -> str:
    for name in names:
        if name in elem.attrib:
            return elem.attrib[name]
    return default


def _parse_datetime(raw: str) -> datetime:
    # dateTime attributes are typically "YYYYMMDD" or "YYYYMMDD;HHMMSS"
    if ";" in raw:
        date_part, time_part = raw.split(";")
        return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    return datetime.strptime(raw, "%Y%m%d")


def parse_flex_report(xml_path: Path) -> tuple[list[ET.Element], list[ET.Element]]:
    """Return (trade_elements, cash_transaction_elements) across all FlexStatements."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    trades = root.findall(".//Trades/Trade")
    cash_txs = root.findall(".//CashTransactions/CashTransaction")
    return trades, cash_txs


def _fx_to_eur(amount: float, currency: str, fx_rate_to_base: str, user_currency: str) -> float:
    if currency == user_currency:
        return amount
    rate = float(fx_rate_to_base) if fx_rate_to_base else 1.0
    return amount * rate


def read_flex(xml_path: Path, user_currency: str) -> dict[str, list[Event]]:
    ticker_events: dict[str, list[Event]] = {}
    trades, _ = parse_flex_report(xml_path)

    for trade in trades:
        ticker = _attr(trade, "symbol")
        name = _attr(trade, "description", default=ticker)
        currency = _attr(trade, "currency", default=user_currency)
        fx_rate = _attr(trade, "fxRateToBase")
        quantity = abs(float(_attr(trade, "quantity", default="0")))
        price = _fx_to_eur(float(_attr(trade, "tradePrice", default="0")), currency, fx_rate, user_currency)
        commission = abs(_fx_to_eur(float(_attr(trade, "ibCommission", default="0")), currency, fx_rate, user_currency))
        date = _parse_datetime(_attr(trade, "dateTime", "tradeDate"))
        buy_sell = _attr(trade, "buySell")
        ev_type = EventType.BUY if buy_sell.upper() == "BUY" else EventType.SELL

        event = Event(evType=ev_type, date=date, num_shares=quantity, value=price, cost_of_transaction=commission)
        ticker_key = f"{ticker}({name})"
        ticker_events.setdefault(ticker_key, []).append(event)

    return ticker_events


def calculate_income_from_flex(
    xml_path: Path, user_currency: str, start_date: datetime, end_date: datetime
) -> tuple[float, dict[str, DividendInfo]]:
    """Parse interest and dividend cash transactions from a Flex Query report,
    filtered to the given date range.

    Returns (total_interest_eur, dividends_by_ticker). US equities are identified
    by ISIN prefix 'US'.
    """
    total_interest = 0.0
    dividends: dict[str, DividendInfo] = {}
    _, cash_txs = parse_flex_report(xml_path)

    for tx in cash_txs:
        tx_date = _parse_datetime(_attr(tx, "dateTime", "settleDate", "reportDate"))
        if not (start_date <= tx_date <= end_date):
            continue

        tx_type = _attr(tx, "type")
        currency = _attr(tx, "currency", default=user_currency)
        fx_rate = _attr(tx, "fxRateToBase")
        amount_eur = _fx_to_eur(float(_attr(tx, "amount", default="0")), currency, fx_rate, user_currency)

        if tx_type in _INTEREST_TYPES:
            total_interest += amount_eur
        elif tx_type in _DIVIDEND_TYPES:
            ticker = _attr(tx, "symbol")
            name = _attr(tx, "description", default=ticker)
            isin = _attr(tx, "isin")
            key = f"{ticker}({name})"
            if key not in dividends:
                dividends[key] = DividendInfo(name=name, is_us=isin.startswith("US"))
            dividends[key].gross_eur += amount_eur
        elif tx_type == "Withholding Tax":
            ticker = _attr(tx, "symbol")
            name = _attr(tx, "description", default=ticker)
            isin = _attr(tx, "isin")
            key = f"{ticker}({name})"
            if key not in dividends:
                dividends[key] = DividendInfo(name=name, is_us=isin.startswith("US"))
            dividends[key].withholding_eur += abs(amount_eur)

    return total_interest, dividends


def import_flex_to_db(xml_path: Path, db_path: str, user_currency: str = "EUR") -> tuple[int, int, int]:
    conn = init_db(db_path)
    trades_added = 0
    interest_added = 0
    dividends_added = 0

    trades, cash_txs = parse_flex_report(xml_path)

    for trade in trades:
        ticker = _attr(trade, "symbol")
        name = _attr(trade, "description", default=ticker)
        currency = _attr(trade, "currency", default=user_currency)
        fx_rate = _attr(trade, "fxRateToBase")
        quantity = abs(float(_attr(trade, "quantity", default="0")))
        price = _fx_to_eur(float(_attr(trade, "tradePrice", default="0")), currency, fx_rate, user_currency)
        commission = abs(_fx_to_eur(float(_attr(trade, "ibCommission", default="0")), currency, fx_rate, user_currency))
        date_raw = _attr(trade, "dateTime", "tradeDate")
        buy_sell = _attr(trade, "buySell")
        event_type = "BUY" if buy_sell.upper() == "BUY" else "SELL"

        trade_id = _attr(trade, "tradeID", "transactionID")
        row_id = f"IBKR-{trade_id}" if trade_id else f"IBKR|{ticker}|{date_raw}|{buy_sell}|{quantity}|{price}"

        time_str = _parse_datetime(date_raw).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO trades (id, time, ticker, name, event_type, num_shares, price_per_share_eur, cost_of_transaction_eur, broker) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'IBKR')",
            (row_id, time_str, ticker, name, event_type, quantity, price, commission)
        )
        trades_added += cursor.rowcount

    for tx in cash_txs:
        tx_type = _attr(tx, "type")
        currency = _attr(tx, "currency", default=user_currency)
        fx_rate = _attr(tx, "fxRateToBase")
        amount_eur = _fx_to_eur(float(_attr(tx, "amount", default="0")), currency, fx_rate, user_currency)
        date_raw = _attr(tx, "dateTime", "settleDate", "reportDate")
        time_str = _parse_datetime(date_raw).strftime("%Y-%m-%d %H:%M:%S")
        tx_id = _attr(tx, "transactionID")
        row_id = f"IBKR-{tx_id}" if tx_id else f"IBKR|{tx_type}|{date_raw}|{amount_eur}"

        if tx_type in _INTEREST_TYPES:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO interest (id, time, amount_eur, broker) VALUES (?, ?, ?, 'IBKR')",
                (row_id, time_str, amount_eur)
            )
            interest_added += cursor.rowcount
        elif tx_type in _DIVIDEND_TYPES or tx_type == "Withholding Tax":
            ticker = _attr(tx, "symbol")
            name = _attr(tx, "description", default=ticker)
            isin = _attr(tx, "isin")
            gross_eur = amount_eur if tx_type in _DIVIDEND_TYPES else 0.0
            withholding_eur = abs(amount_eur) if tx_type == "Withholding Tax" else 0.0
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dividends (id, time, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt, broker) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'IBKR')",
                (row_id, time_str, ticker, name, isin, gross_eur, withholding_eur)
            )
            dividends_added += cursor.rowcount

    conn.commit()
    conn.close()
    return trades_added, interest_added, dividends_added


# --- IBKR "Activity Flex" CSV export (different from the XML Flex Query above) ---
#
# A single CSV file with two concatenated sections, each introduced by its own
# header row (first column "ClientAccountID"): a Trades section (STK and OPT
# rows) and a CashTransactions section (Dividends, Payment in Lieu of
# Dividends, Withholding Tax, Other Fees, Broker Fees Detail, and optionally
# Broker Interest Received/Paid if enabled in the query template).
#
# Each closing trade appears as TWO rows: one with TransactionType="ExchTrade"
# (the real execution — has TradeMoney/IBCommission/NetCash/Taxes) and one with
# TransactionType="" (IBKR's internal FIFO lot-matching detail — has
# OpenDateTime/FifoPnlRealized but zero cash fields). We use only the
# ExchTrade/BookTrade rows and do our own FIFO matching; the blank-type rows
# are skipped to avoid double-counting.
_REAL_TRADE_TYPES = ("ExchTrade", "BookTrade")


def parse_activity_csv(csv_path: Path) -> tuple[list[dict], list[dict]]:
    """Return (trade_rows, cash_transaction_rows), splitting the file on its
    section header rows (each starts with the literal column "ClientAccountID")."""
    trades: list[dict] = []
    cash_txs: list[dict] = []
    header: list[str] | None = None
    section: str | None = None

    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] == "ClientAccountID":
                header = row
                section = "trades" if "Buy/Sell" in row else "cash"
                continue
            if header is None:
                continue
            record = dict(zip(header, row))
            (trades if section == "trades" else cash_txs).append(record)

    return trades, cash_txs


def _activity_fx_to_eur(amount: float, row: dict, user_currency: str) -> float:
    currency = row.get("CurrencyPrimary", user_currency)
    if currency == user_currency:
        return amount
    rate = float(row.get("FXRateToBase") or 1.0)
    return amount * rate


def read_activity_csv(csv_path: Path, user_currency: str) -> dict[str, list[Event]]:
    """Parse STK rows from an Activity CSV export into Event lists, keyed by ticker."""
    ticker_events: dict[str, list[Event]] = {}
    trades, _ = parse_activity_csv(csv_path)

    for row in trades:
        if row.get("AssetClass") != "STK" or row.get("TransactionType") not in _REAL_TRADE_TYPES:
            continue
        ticker = row["Symbol"]
        name = row.get("Description", ticker)
        price = _activity_fx_to_eur(float(row.get("TradePrice") or 0), row, user_currency)
        commission = abs(_activity_fx_to_eur(float(row.get("IBCommission") or 0), row, user_currency))
        quantity = abs(float(row.get("Quantity") or 0))
        date = datetime.strptime(row["TradeDate"], "%Y%m%d")
        ev_type = EventType.BUY if row.get("Buy/Sell") == "BUY" else EventType.SELL

        event = Event(evType=ev_type, date=date, num_shares=quantity, value=price, cost_of_transaction=commission)
        ticker_events.setdefault(f"{ticker}({name})", []).append(event)

    return ticker_events


def read_option_events_from_csv(csv_path: Path, user_currency: str) -> dict[str, list[OptionEvent]]:
    """Parse OPT rows from an Activity CSV export into OptionEvent lists, keyed by contract symbol."""
    contract_events: dict[str, list[OptionEvent]] = {}
    trades, _ = parse_activity_csv(csv_path)

    for row in trades:
        if row.get("AssetClass") != "OPT" or row.get("TransactionType") not in _REAL_TRADE_TYPES:
            continue
        symbol = row["Symbol"].strip()
        description = row.get("Description", symbol)
        net_cash = _activity_fx_to_eur(float(row.get("NetCash") or 0), row, user_currency)
        quantity = abs(float(row.get("Quantity") or 0))
        date = datetime.strptime(row["TradeDate"], "%Y%m%d")
        is_open = row.get("Open/CloseIndicator") == "O"

        event = OptionEvent(date=date, is_open=is_open, quantity=quantity, net_cash=net_cash)
        contract_events.setdefault(f"{symbol}({description})", []).append(event)

    return contract_events


def calculate_income_from_activity_csv(
    csv_path: Path, user_currency: str, start_date: datetime, end_date: datetime
) -> tuple[float, dict[str, DividendInfo]]:
    """Parse interest and dividend cash transactions from an Activity CSV export,
    filtered to the given date range. "Other Fees"/"Broker Fees Detail" rows are
    account-level fees with no CGT/dividend-tax relevance and are ignored.
    """
    total_interest = 0.0
    dividends: dict[str, DividendInfo] = {}
    _, cash_txs = parse_activity_csv(csv_path)

    for row in cash_txs:
        date_str = row.get("Date/Time", "")
        if not date_str:
            continue
        tx_date = datetime.strptime(date_str.split(";")[0], "%Y%m%d")
        if not (start_date <= tx_date <= end_date):
            continue

        tx_type = row.get("Type", "")
        amount_eur = _activity_fx_to_eur(float(row.get("Amount") or 0), row, user_currency)

        if tx_type in _INTEREST_TYPES:
            total_interest += amount_eur
        elif tx_type in _DIVIDEND_TYPES or tx_type == "Withholding Tax":
            ticker = row.get("Symbol", "")
            name = row.get("Description", ticker)
            isin = row.get("ISIN", "")
            key = f"{ticker}({name})"
            if key not in dividends:
                dividends[key] = DividendInfo(name=name, is_us=isin.startswith("US"))
            if tx_type in _DIVIDEND_TYPES:
                dividends[key].gross_eur += amount_eur
            else:
                dividends[key].withholding_eur += abs(amount_eur)

    return total_interest, dividends


def import_activity_csv_to_db(csv_path: Path, db_path: str, user_currency: str = "EUR") -> tuple[int, int, int, int]:
    """Import an Activity CSV export into the local DB cache.

    Returns (trades_added, interest_added, dividends_added, option_legs_added).
    """
    conn = init_db(db_path)
    trades_added = 0
    interest_added = 0
    dividends_added = 0
    options_added = 0

    trades, cash_txs = parse_activity_csv(csv_path)

    for row_index, row in enumerate(trades):
        if row.get("TransactionType") not in _REAL_TRADE_TYPES:
            continue
        symbol = row["Symbol"].strip()
        name = row.get("Description", symbol)
        date = datetime.strptime(row["TradeDate"], "%Y%m%d")
        time_str = date.strftime("%Y-%m-%d %H:%M:%S")
        buy_sell = row.get("Buy/Sell", "")
        quantity = abs(float(row.get("Quantity") or 0))

        # No transactionID column exists in this CSV format, and genuinely
        # duplicate trades (same symbol/date/price) do occur — key dedup on
        # the row's position in the file (stable across re-imports of the
        # same export) rather than its content.
        row_id = f"IBKR-CSV|row{row_index}"

        if row.get("AssetClass") == "STK":
            price = _activity_fx_to_eur(float(row.get("TradePrice") or 0), row, user_currency)
            commission = abs(_activity_fx_to_eur(float(row.get("IBCommission") or 0), row, user_currency))
            event_type = "BUY" if buy_sell == "BUY" else "SELL"
            cursor = conn.execute(
                "INSERT OR IGNORE INTO trades (id, time, ticker, name, event_type, num_shares, price_per_share_eur, cost_of_transaction_eur, broker) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'IBKR')",
                (row_id, time_str, symbol, name, event_type, quantity, price, commission)
            )
            trades_added += cursor.rowcount
        elif row.get("AssetClass") == "OPT":
            net_cash = _activity_fx_to_eur(float(row.get("NetCash") or 0), row, user_currency)
            is_open = 1 if row.get("Open/CloseIndicator") == "O" else 0
            cursor = conn.execute(
                "INSERT OR IGNORE INTO option_trades (id, time, symbol, description, is_open, quantity, net_cash_eur, broker) VALUES (?, ?, ?, ?, ?, ?, ?, 'IBKR')",
                (row_id, time_str, symbol, name, is_open, quantity, net_cash)
            )
            options_added += cursor.rowcount

    for row_index, row in enumerate(cash_txs):
        date_str = row.get("Date/Time", "")
        if not date_str:
            continue
        date = datetime.strptime(date_str.split(";")[0], "%Y%m%d")
        time_str = date.strftime("%Y-%m-%d %H:%M:%S")
        tx_type = row.get("Type", "")
        amount_eur = _activity_fx_to_eur(float(row.get("Amount") or 0), row, user_currency)
        row_id = f"IBKR-CSV|cashtx-row{row_index}"

        if tx_type in _INTEREST_TYPES:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO interest (id, time, amount_eur, broker) VALUES (?, ?, ?, 'IBKR')",
                (row_id, time_str, amount_eur)
            )
            interest_added += cursor.rowcount
        elif tx_type in _DIVIDEND_TYPES or tx_type == "Withholding Tax":
            ticker = row.get("Symbol", "")
            name = row.get("Description", ticker)
            isin = row.get("ISIN", "")
            gross_eur = amount_eur if tx_type in _DIVIDEND_TYPES else 0.0
            withholding_eur = abs(amount_eur) if tx_type == "Withholding Tax" else 0.0
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dividends (id, time, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt, broker) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'IBKR')",
                (row_id, time_str, ticker, name, isin, gross_eur, withholding_eur)
            )
            dividends_added += cursor.rowcount

    conn.commit()
    conn.close()
    return trades_added, interest_added, dividends_added, options_added
