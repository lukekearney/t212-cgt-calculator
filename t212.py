"""Trading 212-specific parsing, import, and API fetching."""
import base64
import csv
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from core import Event, EventType, DividendInfo, init_db


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
                    "INSERT OR IGNORE INTO trades (id, time, ticker, name, event_type, num_shares, price_per_share_eur, cost_of_transaction_eur, broker) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'T212')",
                    (row_id, time_str, ticker, name, event_type, num_shares, price_per_share, cost_of_transaction)
                )
                trades_added += cursor.rowcount

            elif action == 'Interest on cash':
                amount_eur = float(row.get('Total', 0) or 0)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO interest (id, time, amount_eur, broker) VALUES (?, ?, ?, 'T212')",
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
                    "INSERT OR IGNORE INTO dividends (id, time, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt, broker) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'T212')",
                    (div_id, time_str, ticker, name, isin, gross_eur, withholding_eur, is_tax_exempt)
                )
                dividends_added += cursor.rowcount

    conn.commit()
    conn.close()
    return trades_added, interest_added, dividends_added
