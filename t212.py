#!/usr/bin/env python3
import csv
import argparse
import os
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from functools import reduce
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


def calculate_gain_for_ticker(events:list[Event]) -> list[Event]:
    # filters the events to only those relevant for the year
    # remove prior sells. We need to reduce the number of shares owned because they are sold on a first in first out basis for tax calculation
    # if shares were purchased 5 years ago, they must be included, even if they were partially sold in the previous year
    # extract all sell orders before this year and total up the number of shares sold
    all_sells = [ev for ev in events if ev.evType == EventType.SELL]
    all_buys = [ev for ev in events if ev.evType == EventType.BUY]
    sells_this_year = [sale for sale in all_sells if sale.date.year == 2025]
    prior_sells = [sale.num_shares for sale in all_sells if sale.date.year < 2025]
    if prior_sells:
        total_prior_shares_sold = reduce(lambda a, b: a + b, prior_sells)
    else:
        total_prior_shares_sold = 0
    # reduce buys starting from the oldest until total_prior_shares_sold is reached. These buys are still
    # relevant
    start_index = 0
    for buy in all_buys:
        # reduce the num total prior shares count by the number of shares bought prior
        if total_prior_shares_sold >= buy.num_shares:
            total_prior_shares_sold -= buy.num_shares
            start_index += 1
            continue

        if total_prior_shares_sold < buy.num_shares:
            buy.num_shares -= total_prior_shares_sold
            break

    relevant_buys = all_buys[start_index:]

    # get sells this year, and calculate CGT based on oldest shares (earliest buys) first
    # in the future we may do something like storing previous events somewhere so this calculation isn't necessary. 
    # In this case we could just extract the newest event stored and only pull in events from that point onwards
    # we could also prune the database as needed (buys no longer necessary to store)
    buy_index = 0
    sale_index = 0
    cgt = 0
    while sale_index < len(sells_this_year):
        this_sale = sells_this_year[sale_index]
        profit_per_share = this_sale.value - relevant_buys[buy_index].value
        if relevant_buys[buy_index].num_shares > this_sale.num_shares:
            # more buys than those sold. Reduce the number of buys by the number of sales and calculate cgt
            relevant_buys[buy_index].num_shares -= this_sale.num_shares
            cgt += (profit_per_share * this_sale.num_shares) - this_sale.cost_of_transaction
            sale_index += 1
        elif relevant_buys[buy_index].num_shares < this_sale.num_shares:
            cgt += (profit_per_share * relevant_buys[buy_index].num_shares) - relevant_buys[buy_index].cost_of_transaction
            this_sale.num_shares -= relevant_buys[buy_index].num_shares
            buy_index += 1
        else:
            # must be equal. increment both
            cgt += (profit_per_share * this_sale.num_shares) - this_sale.cost_of_transaction
            this_sale.num_shares -= relevant_buys[buy_index].num_shares
            buy_index += 1
            sale_index += 1

    return cgt

def read_csv(csv_path:Path, user_currency:str) -> dict[str, list[Event]]:
    ticker_events = {}

    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            action = row.get('Action')
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
            cost_of_transaction = stamp_duty + currency_fee + french_tax

            # Convert time to datetime
            time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S") if time_str else None

            evType = EventType.BUY if "buy" in action else EventType.SELL
            event = Event(evType=evType, date=time, num_shares=num_shares, value=price_per_share, cost_of_transaction=cost_of_transaction)
            ticker_key = f"{ticker}({name})"
            current_events = ticker_events.get(ticker_key, [])
            current_events.append(event)
            ticker_events[ticker_key] = current_events

    return ticker_events

def get_user_args():
    parser = argparse.ArgumentParser(description="Process T212 CGT calculator arguments.")
    parser.add_argument('--year', type=int, default=datetime.now().year, help='Year for CGT calculation (defaults to current year)')
    parser.add_argument('--csv', type=Path, required=True, help='Path to CSV file containing buy/sell information')
    parser.add_argument('--currency', type=str, default=os.environ.get('T212_CURRENCY', 'EUR'), help='Primary currency (defaults to T212_CURRENCY env var or EUR)')
    parser.add_argument('--rate', type=float, default=os.environ.get('CGT_RATE', '0.33'), help='The rate of CGT tax (defaults to CGT_RATE env var or 0.33)')
    args = parser.parse_args()
    year = args.year
    csv_path = args.csv
    currency = args.currency
    rate = args.rate
    return year, csv_path, currency, rate

if __name__ == "__main__":
    year, csv_path, currency, rate = get_user_args()
    ticker_events = read_csv(csv_path, currency)
    total_gain = 0.0
    for ticker, events in ticker_events.items():
        gain = calculate_gain_for_ticker(events)
        if gain != 0.0:
            print(f"{ticker}: {gain:.2f} {currency}")
        total_gain += gain
    print(f"Total Gain: {total_gain:.2f} {currency}")
    print(f"CGT due: {(total_gain * rate):.2f} {currency}")
