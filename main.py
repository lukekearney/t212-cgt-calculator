#!/usr/bin/env python3
import argparse
import os
from datetime import datetime
from pathlib import Path

import ibkr
import t212
from core import (
    calculate_gain_for_ticker, calculate_option_gain_for_contract, get_deemed_disposals,
    dividend_tax_owed, load_rates,
    load_events_from_db, load_income_from_db, load_option_events_from_db,
)


def get_user_args():
    parser = argparse.ArgumentParser(description="Process T212/IBKR CGT calculator arguments.")
    parser.add_argument('--import-csv', type=Path, metavar='CSV', help='Import a T212 CSV export into the local DB cache')
    parser.add_argument('--import-flex', type=Path, metavar='XML', help='Import an IBKR Flex Query XML report into the local DB cache')
    parser.add_argument('--import-activity-csv', type=Path, metavar='CSV', help='Import an IBKR Activity Flex CSV export (trades incl. options, dividends) into the local DB cache')
    parser.add_argument('--db', type=str, default='./t212.db', help='Path to SQLite cache DB (default: ./t212.db)')
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument('--year', type=int, help='Year for CGT calculation')
    period_group.add_argument('--date-range', nargs=2, metavar=('START', 'END'), help='Start and end date in YYYY-MM-DD format')
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument('--csv', type=Path, help='Path to T212 CSV file containing buy/sell information')
    source_group.add_argument('--flex', type=Path, help='Path to IBKR Flex Query XML report')
    source_group.add_argument('--activity-csv', type=Path, help='Path to IBKR Activity Flex CSV export')
    source_group.add_argument('--api', action='store_true', help='Fetch data from the Trading 212 API')
    parser.add_argument('--api-key', type=str, default=os.environ.get('T212_API_KEY_CG_CALC'), help='Trading 212 API key (defaults to T212_API_KEY env var)')
    parser.add_argument('--currency', type=str, default=os.environ.get('T212_CURRENCY', 'EUR'), help='Primary currency (defaults to T212_CURRENCY env var or EUR)')
    cgt_rate_env = os.environ.get('CGT_RATE')
    parser.add_argument('--rate', type=float, default=float(cgt_rate_env) if cgt_rate_env else None, help='Override CGT rate (default: from rates JSON or CGT_RATE env var)')
    parser.add_argument('--rates-dir', type=str, default='./rates', help='Directory containing annual tax rate JSON files (default: ./rates)')
    args = parser.parse_args()

    if args.api and not args.api_key:
        parser.error("--api requires an API key via --api-key or the T212_API_KEY_CG_CALC environment variable")
    if not args.import_csv and not args.import_flex and not args.import_activity_csv and not args.year and not args.date_range:
        parser.error("one of --year or --date-range is required unless --import-csv/--import-flex/--import-activity-csv is used")

    return args


if __name__ == "__main__":
    args = get_user_args()
    currency = args.currency

    if args.import_csv:
        trades, interest, dividends = t212.import_csv_to_db(args.import_csv, args.db, currency)
        print(f"Imported from T212 CSV: {trades} trade(s), {interest} interest row(s), {dividends} dividend(s)")
    if args.import_flex:
        trades, interest, dividends = ibkr.import_flex_to_db(args.import_flex, args.db, currency)
        print(f"Imported from IBKR Flex report: {trades} trade(s), {interest} interest row(s), {dividends} dividend(s)")
    if args.import_activity_csv:
        trades, interest, dividends, options = ibkr.import_activity_csv_to_db(args.import_activity_csv, args.db, currency)
        print(f"Imported from IBKR Activity CSV: {trades} trade(s), {interest} interest row(s), {dividends} dividend(s), {options} option leg(s)")

    if args.year is not None or args.date_range is not None:
        if args.year is not None:
            start_date = datetime(args.year, 1, 1)
            end_date = datetime(args.year, 12, 31, 23, 59, 59)
        else:
            start_date = datetime.strptime(args.date_range[0], "%Y-%m-%d")
            end_date = datetime.strptime(args.date_range[1], "%Y-%m-%d")

        rates = load_rates(end_date.year, args.rates_dir)
        if args.rate is not None:
            rates['cgt_rate'] = args.rate

        option_events = {}
        if args.api:
            api_secret = os.environ.get("T212_API_SECRET_CG_CALC")
            ticker_events = t212.fetch_from_api(args.api_key, api_secret, currency, end_date)
        elif args.csv:
            ticker_events = t212.read_csv(args.csv, currency)
        elif args.flex:
            ticker_events = ibkr.read_flex(args.flex, currency)
        elif args.activity_csv:
            ticker_events = ibkr.read_activity_csv(args.activity_csv, currency)
            option_events = ibkr.read_option_events_from_csv(args.activity_csv, currency)
        else:
            ticker_events = load_events_from_db(args.db)
            option_events = load_option_events_from_db(args.db)

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

        if option_events:
            print(f"\n--- Options ---")
            for contract, events in option_events.items():
                try:
                    gain = calculate_option_gain_for_contract(events, start_date=start_date, end_date=end_date)
                except ValueError as e:
                    print(f"Warning: skipping {contract} — {e}")
                    continue
                if gain != 0.0:
                    print(f"{contract}: {gain:.2f} {currency}")
                total_gain += gain

        cgt_rate = rates['cgt_rate']
        print(f"\nTotal Gain: {total_gain:.2f} {currency}")
        print(f"CGT due: {(total_gain * cgt_rate):.2f} {currency}")

        if not args.api:
            if args.csv:
                total_interest, income_dividends = t212.calculate_income_from_csv(args.csv, currency, start_date, end_date)
            elif args.flex:
                total_interest, income_dividends = ibkr.calculate_income_from_flex(args.flex, currency, start_date, end_date)
            elif args.activity_csv:
                total_interest, income_dividends = ibkr.calculate_income_from_activity_csv(args.activity_csv, currency, start_date, end_date)
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
