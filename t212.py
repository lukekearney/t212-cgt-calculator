#!/usr/bin/env python3
import base64
import csv
import argparse
import os
import json
import sqlite3
import time
import urllib.request
import urllib.error
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta
from pathlib import Path

class EventType(Enum):
    BUY = 0
    SELL = 1


@dataclass
class Event:
    evType:EventType
    date:datetime
    num_shares:float
    value:float
    cost_of_transaction:float = 0.0


_EPSILON = 1e-9

_DEFAULT_RATES = {
    "cgt_rate": 0.33,
    "etf_exit_tax_rate": 0.41,
    "income_tax_rate": 0.52,
    "us_withholding_treaty_credit": 0.15,
    "interest_dirt_rate": 0.33,
}


def _consume(lots: list, needed: float, sell_price: float) -> tuple[float, float, float]:
    """Consume up to `needed` shares from lots (modifying num_shares in place).
    Returns (gain, buy_costs_freed, shares_consumed)."""
    gain = buy_costs = consumed = 0.0
    for lot in lots:
        if lot.num_shares <= _EPSILON or needed <= _EPSILON:
            continue
        matched = min(lot.num_shares, needed)
        gain += (sell_price - lot.value) * matched
        lot.num_shares -= matched
        if lot.num_shares <= _EPSILON:
            buy_costs += lot.cost_of_transaction
            lot.num_shares = 0.0
        needed -= matched
        consumed += matched
    return gain, buy_costs, consumed


def calculate_gain_for_ticker(events: list[Event], start_date: datetime, end_date: datetime) -> float:
    """Calculate CGT for a single ticker using Irish share-matching rules (s.580 TCA 1997 / Revenue 6A.3.1):
    1. Same-day acquisitions matched first.
    2. Acquisitions within 4 weeks (28 days) BEFORE the disposal (Revenue 6A.3.1 backward rule).
    3. Acquisitions within 4 weeks (28 days) AFTER the disposal (anti-bed-and-breakfast).
    4. FIFO from the remaining historical pool.
    """
    events_sorted = sorted(events, key=lambda e: e.date)
    lots = [deepcopy(e) for e in events_sorted if e.evType == EventType.BUY and e.date <= end_date]
    sells = [deepcopy(e) for e in events_sorted if e.evType == EventType.SELL and start_date <= e.date <= end_date]
    prior_sells = [e for e in events_sorted if e.evType == EventType.SELL and e.date < start_date]

    # Burn prior sells through FIFO pool (no CGT tracking needed for prior periods)
    prior_total = sum(e.num_shares for e in prior_sells)
    for lot in lots:
        if prior_total <= _EPSILON:
            break
        c = min(lot.num_shares, prior_total)
        lot.num_shares -= c
        prior_total -= c
    if prior_total > _EPSILON:
        raise ValueError(
            "Ran out of buy events matching prior sells. "
            "This may indicate shares acquired via corporate action, stock split, or transfer "
            "that don't appear as buy orders in the available history."
        )

    cgt = 0.0
    for sell in sells:
        remaining = sell.num_shares
        sell_day = sell.date.date()
        bwd_cutoff = (sell.date - timedelta(days=28)).date()
        fwd_cutoff = (sell.date + timedelta(days=28)).date()

        # Priority 1: same-day acquisitions
        g, bc, c = _consume(
            [lot for lot in lots if lot.date.date() == sell_day and lot.num_shares > _EPSILON],
            remaining, sell.value
        )
        cgt += g - bc; remaining -= c

        # Priority 2: acquisitions within 4 weeks before disposal (Revenue 6A.3.1)
        if remaining > _EPSILON:
            g, bc, c = _consume(
                [lot for lot in lots if bwd_cutoff <= lot.date.date() < sell_day and lot.num_shares > _EPSILON],
                remaining, sell.value
            )
            cgt += g - bc; remaining -= c

        # Priority 3: acquisitions within 4 weeks after disposal (anti-bed-and-breakfast)
        if remaining > _EPSILON:
            g, bc, c = _consume(
                [lot for lot in lots if sell_day < lot.date.date() <= fwd_cutoff and lot.num_shares > _EPSILON],
                remaining, sell.value
            )
            cgt += g - bc; remaining -= c

        # Priority 4: FIFO from historical pool (older than 28 days before disposal)
        if remaining > _EPSILON:
            g, bc, c = _consume(
                [lot for lot in lots if lot.date.date() < bwd_cutoff and lot.num_shares > _EPSILON],
                remaining, sell.value
            )
            cgt += g - bc; remaining -= c

        if remaining > _EPSILON:
            raise ValueError(
                f"Ran out of buy events with 1 sell(s) remaining. "
                "This may indicate shares acquired via corporate action, stock split, or transfer "
                "that don't appear as buy orders in the available history."
            )

        cgt -= sell.cost_of_transaction

    return cgt

def get_deemed_disposals(
    events: list[Event], start_date: datetime, end_date: datetime
) -> list[tuple[datetime, float, float]]:
    """Identify exit-tax-fund lots whose 8-year deemed disposal anniversary falls within the period.
    Applies FIFO to determine remaining shares after actual sales.
    Returns list of (anniversary_date, remaining_shares, cost_per_share), sorted by date.
    """
    events_sorted = sorted(events, key=lambda e: e.date)
    lots = [deepcopy(e) for e in events_sorted if e.evType == EventType.BUY]
    sells = [e for e in events_sorted if e.evType == EventType.SELL]

    # Apply all sells FIFO to track remaining shares per lot
    for sell in sells:
        remaining = sell.num_shares
        for lot in lots:
            if lot.num_shares <= _EPSILON or remaining <= _EPSILON:
                continue
            consumed = min(lot.num_shares, remaining)
            lot.num_shares -= consumed
            remaining -= consumed

    result = []
    for lot in lots:
        if lot.num_shares <= _EPSILON:
            continue
        try:
            anniversary = lot.date.replace(year=lot.date.year + 8)
        except ValueError:
            # Feb 29 in a non-leap year → Feb 28
            anniversary = lot.date.replace(year=lot.date.year + 8, day=28)
        if start_date <= anniversary <= end_date:
            result.append((anniversary, lot.num_shares, lot.value))

    return sorted(result, key=lambda x: x[0])


@dataclass
class DividendInfo:
    name: str
    gross_eur: float = 0.0
    withholding_eur: float = 0.0
    is_us: bool = False
    is_etf: bool = False
    is_tax_exempt: bool = False


def dividend_tax_owed(div: DividendInfo, rates: dict) -> float:
    if div.is_tax_exempt:
        return 0.0
    etf_rate = rates.get('etf_exit_tax_rate', _DEFAULT_RATES['etf_exit_tax_rate'])
    income_rate = rates.get('income_tax_rate', _DEFAULT_RATES['income_tax_rate'])
    us_credit = rates.get('us_withholding_treaty_credit', _DEFAULT_RATES['us_withholding_treaty_credit'])
    if div.is_etf:
        return div.gross_eur * etf_rate
    if div.is_us:
        return div.gross_eur * (income_rate - us_credit)
    return max(0.0, div.gross_eur * income_rate - div.withholding_eur)


def read_csv(csv_path: Path, user_currency: str) -> dict[str, list[Event]]:
    ticker_events = {}

    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            action = row.get('Action', '')
            # Only process buy/sell rows; dividends/interest handled separately
            if 'buy' not in action.lower() and 'sell' not in action.lower():
                continue

            time_str = row.get('Time')
            ticker = row.get('Ticker')
            name = row.get('Name')
            num_shares = float(row.get('No. of shares', 0))
            price_per_share = float(row.get('Price / share', 0))
            currency = row.get('Currency (Price / share)')
            exchange_rate = float(row.get('Exchange rate', 1))
            if currency != user_currency:
                price_per_share *= exchange_rate

            # Transaction costs
            stamp_duty = float(row.get('Stamp duty reserve tax', 0) or 0)
            currency_fee = float(row.get('Currency conversion fee', 0) or 0)
            french_tax = float(row.get('French transaction tax', 0) or 0)
            finra_fee = float(row.get('Finra fee', 0) or 0)
            cost_of_transaction = stamp_duty + currency_fee + french_tax + finra_fee

            time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S") if time_str else None

            evType = EventType.BUY if "buy" in action.lower() else EventType.SELL
            event = Event(evType=evType, date=time, num_shares=num_shares, value=price_per_share, cost_of_transaction=cost_of_transaction)
            ticker_key = f"{ticker}({name})"
            current_events = ticker_events.get(ticker_key, [])
            current_events.append(event)
            ticker_events[ticker_key] = current_events

    return ticker_events


def calculate_income_from_csv(
    csv_path: Path, user_currency: str, start_date: datetime, end_date: datetime
) -> tuple[float, dict[str, DividendInfo]]:
    """Parse interest and dividend rows from the CSV, filtered to the given date range.

    Returns (total_interest_eur, dividends_by_ticker).
    US equities are identified by ISIN prefix 'US'. ETF detection is not yet automatic —
    set is_etf manually or extend this function if needed.
    """
    total_interest = 0.0
    dividends: dict[str, DividendInfo] = {}

    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            action = row.get('Action', '')
            time_str = row.get('Time', '')
            if not time_str:
                continue
            row_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            if not (start_date <= row_time <= end_date):
                continue

            if action == 'Interest on cash':
                total_interest += float(row.get('Total', 0) or 0)

            elif action.startswith('Dividend'):
                ticker = row.get('Ticker', '')
                name = row.get('Name', '')
                isin = row.get('ISIN', '')
                exchange_rate = float(row.get('Exchange rate', 1) or 1)
                gross_eur = float(row.get('Total', 0) or 0)
                withholding = float(row.get('Withholding tax', 0) or 0)
                withholding_eur = withholding * exchange_rate

                is_tax_exempt = (action == 'Dividend (Tax exempted)')
                is_us = isin.startswith('US')

                key = f"{ticker}({name})"
                if key not in dividends:
                    dividends[key] = DividendInfo(name=name, is_us=is_us, is_tax_exempt=is_tax_exempt)
                dividends[key].gross_eur += gross_eur
                dividends[key].withholding_eur += withholding_eur

    return total_interest, dividends

def load_rates(year: int, rates_dir: str = './rates') -> dict:
    """Load tax rates from rates/<year>.json, falling back to the highest available year."""
    rates_path = Path(rates_dir)
    if not rates_path.exists():
        print(f"Note: rates directory '{rates_dir}' not found, using built-in defaults.")
        return dict(_DEFAULT_RATES)
    available = sorted(int(f.stem) for f in rates_path.glob('*.json') if f.stem.isdigit())
    if not available:
        print(f"Note: no rate files found in '{rates_dir}', using built-in defaults.")
        return dict(_DEFAULT_RATES)
    use_year = year if year in available else max(available)
    if use_year != year:
        print(f"Note: no rates file for {year}, using {use_year}.json")
    with open(rates_path / f'{use_year}.json') as f:
        return {**_DEFAULT_RATES, **json.load(f)}


def build_auth_header(api_key: str, api_secret: str):
    encoded = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return f"Basic {encoded}"

def fetch_from_api(api_key: str, api_secret: str, user_currency: str, end_date: datetime) -> dict[str, list[Event]]:
    """Fetch all historical filled orders from the Trading 212 API up to end_date.
    NOTE THIS DOESN'T WORK

    We fetch the full history (not just the requested period) because sells within the
    period may have cost basis from buys made much earlier. Items after end_date are
    skipped since they cannot affect the CGT calculation.
    """
    base_url = "https://live.trading212.com/api/v0"
    auth_header = build_auth_header(api_key, api_secret)
    headers = {"Authorization": auth_header}

    ticker_events: dict[str, list[Event]] = {}
    # limit=50 is the API maximum page size; pagination handles fetching all results
    url = f"{base_url}/equity/history/orders?limit=50"

    while url:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("Rate limited by API, waiting 60 seconds...")
                time.sleep(60)
                continue
            raise

        for item in data.get("items", []):
            order = item.get("order", {})
            fill = item.get("fill", {})

            if not fill or order.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
                continue

            filled_at_str = fill.get("filledAt")
            if not filled_at_str:
                continue
            filled_at = datetime.fromisoformat(filled_at_str.replace("Z", "+00:00")).replace(tzinfo=None)

            # Skip transactions after end_date — they cannot affect CGT for the period
            if filled_at > end_date:
                continue

            instrument = order.get("instrument", {})
            ticker = instrument.get("ticker", order.get("ticker", ""))
            name = instrument.get("name", "")

            price = float(fill.get("price", 0.0))
            quantity = abs(float(fill.get("quantity", 0.0)))

            instrument_currency = instrument.get("currency", order.get("currency", user_currency))
            wallet_impact = fill.get("walletImpact", {})
            fx_rate = float(wallet_impact.get("fxRate") or 1.0)
            if instrument_currency != user_currency:
                price *= fx_rate

            taxes = wallet_impact.get("taxes", [])
            cost_of_transaction = sum(abs(float(t.get("quantity", 0.0))) for t in taxes)

            ev_type = EventType.BUY if order.get("side") == "BUY" else EventType.SELL
            event = Event(evType=ev_type, date=filled_at, num_shares=quantity, value=price, cost_of_transaction=cost_of_transaction)

            ticker_key = f"{ticker}({name})"
            events = ticker_events.get(ticker_key, [])
            events.append(event)
            ticker_events[ticker_key] = events

        next_path = data.get("nextPagePath")
        url = f"https://live.trading212.com{next_path}" if next_path else None

    return ticker_events


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            time TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            num_shares REAL NOT NULL,
            price_per_share_eur REAL NOT NULL,
            cost_of_transaction_eur REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS interest (
            id TEXT PRIMARY KEY,
            time TEXT NOT NULL,
            amount_eur REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dividends (
            id TEXT PRIMARY KEY,
            time TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            isin TEXT NOT NULL,
            gross_eur REAL NOT NULL,
            withholding_eur REAL NOT NULL DEFAULT 0.0,
            is_tax_exempt INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    return conn


def import_csv_to_db(csv_path: Path, db_path: str, user_currency: str) -> tuple[int, int, int]:
    conn = init_db(db_path)
    trades_added = 0
    interest_added = 0
    dividends_added = 0

    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            action = row.get('Action', '')
            time_str = row.get('Time', '')
            if not time_str:
                continue
            row_id = row.get('ID', '')

            if 'buy' in action.lower() or 'sell' in action.lower() or action == 'Equity rights':
                ticker = row.get('Ticker', '')
                name = row.get('Name', '')
                num_shares = float(row.get('No. of shares', 0) or 0)
                price_per_share = float(row.get('Price / share', 0) or 0)
                currency = row.get('Currency (Price / share)', user_currency)
                exchange_rate = float(row.get('Exchange rate', 1) or 1)
                if currency != user_currency:
                    price_per_share *= exchange_rate
                stamp_duty = float(row.get('Stamp duty reserve tax', 0) or 0)
                currency_fee = float(row.get('Currency conversion fee', 0) or 0)
                french_tax = float(row.get('French transaction tax', 0) or 0)
                finra_fee = float(row.get('Finra fee', 0) or 0)
                cost_of_transaction = stamp_duty + currency_fee + french_tax + finra_fee
                event_type = 'SELL' if 'sell' in action.lower() else 'BUY'
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO trades (id, time, ticker, name, event_type, num_shares, price_per_share_eur, cost_of_transaction_eur) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, time_str, ticker, name, event_type, num_shares, price_per_share, cost_of_transaction)
                )
                trades_added += cursor.rowcount

            elif action == 'Interest on cash':
                amount_eur = float(row.get('Total', 0) or 0)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO interest (id, time, amount_eur) VALUES (?, ?, ?)",
                    (row_id, time_str, amount_eur)
                )
                interest_added += cursor.rowcount

            elif action.startswith('Dividend'):
                ticker = row.get('Ticker', '')
                name = row.get('Name', '')
                isin = row.get('ISIN', '')
                exchange_rate = float(row.get('Exchange rate', 1) or 1)
                gross_eur = float(row.get('Total', 0) or 0)
                withholding = float(row.get('Withholding tax', 0) or 0)
                withholding_eur = withholding * exchange_rate
                is_tax_exempt = 1 if action == 'Dividend (Tax exempted)' else 0
                div_id = f"{time_str}|{ticker}|{action}"
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO dividends (id, time, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (div_id, time_str, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt)
                )
                dividends_added += cursor.rowcount

    conn.commit()
    conn.close()
    return trades_added, interest_added, dividends_added


def load_events_from_db(db_path: str) -> dict[str, list[Event]]:
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT ticker, name, event_type, time, num_shares, price_per_share_eur, cost_of_transaction_eur FROM trades ORDER BY time ASC"
    )
    ticker_events: dict[str, list[Event]] = {}
    for row in cursor.fetchall():
        ticker, name, event_type, time_str, num_shares, price_per_share, cost_of_transaction = row
        ev_type = EventType.BUY if event_type == 'BUY' else EventType.SELL
        date = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        event = Event(evType=ev_type, date=date, num_shares=num_shares, value=price_per_share, cost_of_transaction=cost_of_transaction)
        ticker_events.setdefault(f"{ticker}({name})", []).append(event)
    conn.close()
    return ticker_events


def load_income_from_db(db_path: str, start_date: datetime, end_date: datetime) -> tuple[float, dict[str, DividendInfo]]:
    conn = sqlite3.connect(db_path)
    start_str = start_date.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_date.strftime("%Y-%m-%d %H:%M:%S")

    row = conn.execute(
        "SELECT SUM(amount_eur) FROM interest WHERE time >= ? AND time <= ?",
        (start_str, end_str)
    ).fetchone()
    total_interest = row[0] or 0.0

    cursor = conn.execute(
        "SELECT ticker, name, MAX(isin), SUM(gross_eur), SUM(withholding_eur), MAX(is_tax_exempt) FROM dividends WHERE time >= ? AND time <= ? GROUP BY ticker, name",
        (start_str, end_str)
    )
    dividends: dict[str, DividendInfo] = {}
    for ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt in cursor.fetchall():
        is_us = (isin or '').startswith('US')
        dividends[f"{ticker}({name})"] = DividendInfo(
            name=name,
            gross_eur=gross_eur or 0.0,
            withholding_eur=withholding_eur or 0.0,
            is_us=is_us,
            is_tax_exempt=bool(is_tax_exempt),
        )

    conn.close()
    return total_interest, dividends


def get_user_args():
    parser = argparse.ArgumentParser(description="Process T212 CGT calculator arguments.")
    parser.add_argument('--import-csv', type=Path, metavar='CSV', help='Import a CSV file into the local DB cache')
    parser.add_argument('--db', type=str, default='./t212.db', help='Path to SQLite cache DB (default: ./t212.db)')
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument('--year', type=int, help='Year for CGT calculation')
    period_group.add_argument('--date-range', nargs=2, metavar=('START', 'END'), help='Start and end date in YYYY-MM-DD format')
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument('--csv', type=Path, help='Path to CSV file containing buy/sell information')
    source_group.add_argument('--api', action='store_true', help='Fetch data from the Trading 212 API')
    parser.add_argument('--api-key', type=str, default=os.environ.get('T212_API_KEY_CG_CALC'), help='Trading 212 API key (defaults to T212_API_KEY env var)')
    parser.add_argument('--currency', type=str, default=os.environ.get('T212_CURRENCY', 'EUR'), help='Primary currency (defaults to T212_CURRENCY env var or EUR)')
    cgt_rate_env = os.environ.get('CGT_RATE')
    parser.add_argument('--rate', type=float, default=float(cgt_rate_env) if cgt_rate_env else None, help='Override CGT rate (default: from rates JSON or CGT_RATE env var)')
    parser.add_argument('--rates-dir', type=str, default='./rates', help='Directory containing annual tax rate JSON files (default: ./rates)')
    args = parser.parse_args()

    if args.api and not args.api_key:
        parser.error("--api requires an API key via --api-key or the T212_API_KEY_CG_CALC environment variable")
    if not args.import_csv and not args.year and not args.date_range:
        parser.error("one of --year or --date-range is required unless --import-csv is used")

    return args

if __name__ == "__main__":
    args = get_user_args()
    currency = args.currency

    if args.import_csv:
        trades, interest, dividends = import_csv_to_db(args.import_csv, args.db, currency)
        print(f"Imported: {trades} trade(s), {interest} interest row(s), {dividends} dividend(s)")
    else:
        if args.year is not None:
            start_date = datetime(args.year, 1, 1)
            end_date = datetime(args.year, 12, 31, 23, 59, 59)
        else:
            start_date = datetime.strptime(args.date_range[0], "%Y-%m-%d")
            end_date = datetime.strptime(args.date_range[1], "%Y-%m-%d")

        rates = load_rates(end_date.year, args.rates_dir)
        if args.rate is not None:
            rates['cgt_rate'] = args.rate

        if args.api:
            api_secret = os.environ.get("T212_API_SECRET_CG_CALC")
            ticker_events = fetch_from_api(args.api_key, api_secret, currency, end_date)
        elif args.csv:
            ticker_events = read_csv(args.csv, currency)
        else:
            ticker_events = load_events_from_db(args.db)

        total_gain = 0.0
        for ticker, events in ticker_events.items():
            try:
                gain = calculate_gain_for_ticker(events, start_date=start_date, end_date=end_date)
            except ValueError as e:
                print(f"Warning: skipping {ticker} — {e}")
                continue
            if gain != 0.0:
                print(f"{ticker}: {gain:.2f} {currency}")
            total_gain += gain
        cgt_rate = rates['cgt_rate']
        print(f"Total Gain: {total_gain:.2f} {currency}")
        print(f"CGT due: {(total_gain * cgt_rate):.2f} {currency}")

        if not args.api:
            if args.csv:
                total_interest, income_dividends = calculate_income_from_csv(args.csv, currency, start_date, end_date)
            else:
                total_interest, income_dividends = load_income_from_db(args.db, start_date, end_date)

            dirt_rate = rates['interest_dirt_rate']
            print(f"\n--- Interest ---")
            print(f"Total interest: {total_interest:.2f} {currency}")
            print(f"Tax owed ({dirt_rate*100:.0f}%): {total_interest * dirt_rate:.2f} {currency}")

            if income_dividends:
                print(f"\n--- Dividends ---")
                total_div_gross = 0.0
                total_div_tax = 0.0
                for ticker_key, div in income_dividends.items():
                    tax = dividend_tax_owed(div, rates)
                    if div.is_tax_exempt:
                        tag = " [tax-exempt]"
                    elif div.is_etf:
                        tag = " [ETF]"
                    elif div.is_us:
                        tag = " [US equity]"
                    else:
                        tag = ""
                    print(f"  {ticker_key}{tag}: {div.gross_eur:.2f} {currency} gross → {tax:.2f} {currency} tax owed")
                    total_div_gross += div.gross_eur
                    total_div_tax += tax
                print(f"Total dividends: {total_div_gross:.2f} {currency}")
                print(f"Total dividend tax owed: {total_div_tax:.2f} {currency}")

            exit_tax_tickers = rates.get('exit_tax_fund_tickers', [])
            if exit_tax_tickers:
                etf_rate = rates['etf_exit_tax_rate']
                triggered = []
                for ticker_key, events in ticker_events.items():
                    ticker = ticker_key.split('(')[0]
                    if ticker in exit_tax_tickers:
                        for anniversary, shares, cost in get_deemed_disposals(events, start_date, end_date):
                            triggered.append((ticker_key, anniversary, shares, cost))
                if triggered:
                    print(f"\n--- Deemed Disposal (8-year rule, {etf_rate*100:.0f}% exit tax) ---")
                    for ticker_key, anniversary, shares, cost in sorted(triggered, key=lambda x: x[1]):
                        print(f"  {ticker_key}: {shares:.6f} shares at cost {cost:.4f} {currency}/share — anniversary {anniversary.date()}")
                        print(f"    Apply {etf_rate*100:.0f}% exit tax to (NAV on {anniversary.date()} − {cost:.4f}) × {shares:.6f}")
