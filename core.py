import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


class EventType(Enum):
    BUY = 0
    SELL = 1


@dataclass
class Event:
    evType: EventType
    date: datetime
    num_shares: float
    value: float
    cost_of_transaction: float = 0.0


@dataclass
class OptionEvent:
    """A single opening or closing leg of an options contract.

    `net_cash` is the total signed cash flow for this leg (negative = cash
    paid out, positive = cash received), inclusive of commissions/taxes.
    Expiry (abandonment) is represented as a closing leg with net_cash=0.0.
    """
    date: datetime
    is_open: bool
    quantity: float
    net_cash: float


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


def calculate_option_gain_for_contract(
    events: list[OptionEvent], start_date: datetime, end_date: datetime
) -> float:
    """Calculate CGT on a single option contract (TCA 1997 s.541).

    Options are a distinct asset class from shares — the same-day/4-week
    share-identification rules (s.581) don't apply, so this is plain FIFO
    with no date-window matching. Each closing leg (including expiry, which
    is a closing leg with net_cash=0.0 — full loss to the holder, full gain
    to the writer) is matched against the oldest remaining opening leg(s).
    The gain on the matched quantity is the sum of the opening and closing
    legs' net cash flow (each already signed: negative = paid, positive =
    received), which naturally handles long and short positions, partial
    fills, and expiry without separate-case logic.
    """
    events_sorted = sorted(events, key=lambda e: e.date)
    opens = [{"remaining": e.quantity, "unit_cash": e.net_cash / e.quantity} for e in events_sorted if e.is_open]
    closes = [e for e in events_sorted if not e.is_open]

    gain = 0.0
    for close in closes:
        remaining = close.quantity
        unit_cash = close.net_cash / close.quantity
        for lot in opens:
            if lot["remaining"] <= _EPSILON or remaining <= _EPSILON:
                continue
            matched = min(lot["remaining"], remaining)
            if start_date <= close.date <= end_date:
                gain += (lot["unit_cash"] + unit_cash) * matched
            lot["remaining"] -= matched
            remaining -= matched
        if remaining > _EPSILON:
            raise ValueError(
                "Ran out of opening legs with a closing leg remaining. "
                "This may indicate an option opened before the available history."
            )

    return gain


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
        CREATE TABLE IF NOT EXISTS option_trades (
            id TEXT PRIMARY KEY,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            description TEXT NOT NULL,
            is_open INTEGER NOT NULL,
            quantity REAL NOT NULL,
            net_cash_eur REAL NOT NULL,
            broker TEXT NOT NULL DEFAULT 'IBKR'
        );
    """)
    for table in ("trades", "interest", "dividends"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN broker TEXT NOT NULL DEFAULT 'T212'")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


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


def load_option_events_from_db(db_path: str) -> dict[str, list[OptionEvent]]:
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT symbol, description, is_open, time, quantity, net_cash_eur FROM option_trades ORDER BY time ASC"
    )
    contract_events: dict[str, list[OptionEvent]] = {}
    for row in cursor.fetchall():
        symbol, description, is_open, time_str, quantity, net_cash = row
        date = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        event = OptionEvent(date=date, is_open=bool(is_open), quantity=quantity, net_cash=net_cash)
        contract_events.setdefault(f"{symbol}({description})", []).append(event)
    conn.close()
    return contract_events


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
