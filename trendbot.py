#!/usr/bin/env python3
"""TrendBot: a simple trend-following market-timing bot.

Rule: be invested when the market is in an uptrend, sit in cash when it is in
a downtrend. "Uptrend" means the closing price is above its N-day simple moving
average (200 days by default). A small buffer band around the average keeps the
bot from flipping in and out every time price wiggles across the line.

Commands:
  signal    Show today's IN / OUT signal for a ticker.
  backtest  Replay the rule over history and compare it to buy-and-hold.
  trade     Place orders on Alpaca to match the signal (paper account by default).

Price data comes from Yahoo Finance, or from a CSV file you supply with --csv
(columns: Date, Close).

This is a tool, not financial advice. Trend following tends to reduce deep
drawdowns but also lags turns, gets whipsawed in choppy markets, and often
trails buy-and-hold in strong bull markets.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass

import pandas as pd
import requests

TRADING_DAYS = 252
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_csv(path: str) -> pd.Series:
    """Load daily closes from a CSV with Date and Close (or Adj Close) columns."""
    df = pd.read_csv(path)
    cols = {c.lower().replace(" ", "_"): c for c in df.columns}
    date_col = cols.get("date")
    close_col = cols.get("adj_close") or cols.get("close")
    if date_col is None or close_col is None:
        raise ValueError(f"{path}: need a Date column and a Close or Adj Close column")
    closes = pd.Series(df[close_col].astype(float).values,
                       index=pd.to_datetime(df[date_col]), name="close")
    return closes.sort_index().dropna()


def fetch_yahoo(ticker: str, period: str = "max") -> pd.Series:
    """Download daily adjusted closes from Yahoo Finance."""
    resp = requests.get(
        YAHOO_URL.format(ticker=ticker),
        params={"range": period, "interval": "1d", "events": "div,splits"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    timestamps = result["timestamp"]
    adj = result["indicators"].get("adjclose")
    prices = adj[0]["adjclose"] if adj else result["indicators"]["quote"][0]["close"]
    index = pd.to_datetime(timestamps, unit="s").normalize()
    return pd.Series(prices, index=index, name="close", dtype=float).dropna()


def get_prices(args: argparse.Namespace) -> pd.Series:
    if args.csv:
        return load_csv(args.csv)
    return fetch_yahoo(args.ticker)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def compute_signals(closes: pd.Series, window: int = 200, band: float = 0.01) -> pd.DataFrame:
    """Return a frame with close, sma and position (1 = invested, 0 = cash).

    Go IN when close > sma * (1 + band); go OUT when close < sma * (1 - band);
    inside the band, keep whatever position we already had.
    """
    if len(closes) < window:
        raise ValueError(f"need at least {window} days of prices, got {len(closes)}")
    sma = closes.rolling(window).mean()
    position = []
    state = 0
    for close, avg in zip(closes.values, sma.values):
        if not math.isnan(avg):
            if close > avg * (1 + band):
                state = 1
            elif close < avg * (1 - band):
                state = 0
        position.append(state)
    return pd.DataFrame({"close": closes, "sma": sma, "position": position}, index=closes.index)


@dataclass
class Stats:
    total_return: float
    cagr: float
    volatility: float
    max_drawdown: float
    sharpe: float


def performance(equity: pd.Series) -> Stats:
    daily = equity.pct_change().dropna()
    years = len(daily) / TRADING_DAYS
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
    vol = daily.std() * math.sqrt(TRADING_DAYS)
    drawdown = (equity / equity.cummax() - 1).min()
    sharpe = daily.mean() / daily.std() * math.sqrt(TRADING_DAYS) if daily.std() > 0 else 0.0
    return Stats(total, cagr, vol, drawdown, sharpe)


def backtest(signals: pd.DataFrame, cash_rate: float = 0.0, cost: float = 0.0005) -> pd.DataFrame:
    """Simulate the rule. Signals are acted on at the NEXT day's close (no peeking).

    cash_rate: annual yield earned while out of the market (e.g. 0.04 for T-bills).
    cost: fraction of equity lost on each switch (slippage + fees).
    """
    sig = signals.dropna(subset=["sma"]).copy()
    held = sig["position"].shift(1).fillna(0)          # position held during each day
    market_ret = sig["close"].pct_change().fillna(0)
    cash_ret = (1 + cash_rate) ** (1 / TRADING_DAYS) - 1
    switches = held.diff().abs().fillna(0)
    strat_ret = held * market_ret + (1 - held) * cash_ret - switches * cost
    sig["held"] = held
    sig["strategy"] = (1 + strat_ret).cumprod()
    sig["buy_hold"] = (1 + market_ret).cumprod()
    return sig


# ---------------------------------------------------------------------------
# Broker (Alpaca)
# ---------------------------------------------------------------------------

class Alpaca:
    def __init__(self, live: bool = False):
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")
        self.base = ALPACA_LIVE_URL if live else ALPACA_PAPER_URL
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _request(self, method: str, path: str, **kwargs):
        resp = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def position_qty(self, symbol: str) -> float:
        pos = self._request("GET", f"/v2/positions/{symbol}")
        return float(pos["qty"]) if pos else 0.0

    def buy_notional(self, symbol: str, dollars: float) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol, "notional": f"{dollars:.2f}", "side": "buy",
            "type": "market", "time_in_force": "day",
        })

    def sell_all(self, symbol: str) -> dict:
        return self._request("DELETE", f"/v2/positions/{symbol}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def cmd_signal(args: argparse.Namespace) -> int:
    sig = compute_signals(get_prices(args), args.window, args.band)
    last = sig.iloc[-1]
    gap = last["close"] / last["sma"] - 1
    state = "IN  (uptrend - stay invested)" if last["position"] else "OUT (downtrend - stay in cash)"
    print(f"{args.ticker} as of {sig.index[-1].date()}")
    print(f"  close:        {last['close']:.2f}")
    print(f"  {args.window}-day avg:  {last['sma']:.2f}  ({pct(gap)} vs avg)")
    print(f"  buffer band:  ±{args.band * 100:.1f}%  "
          f"(IN above {last['sma'] * (1 + args.band):.2f}, OUT below {last['sma'] * (1 - args.band):.2f})")
    print(f"  signal:       {state}")
    flips = sig["position"].diff().fillna(0) != 0
    if flips.any():
        print(f"  last switch:  {sig.index[flips][-1].date()}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    sig = compute_signals(get_prices(args), args.window, args.band)
    result = backtest(sig, args.cash_rate, args.cost)
    if args.start:
        result = result[result.index >= pd.Timestamp(args.start)].copy()
        result[["strategy", "buy_hold"]] = result[["strategy", "buy_hold"]] / result[["strategy", "buy_hold"]].iloc[0]
    if len(result) < 2:
        raise SystemExit("Not enough data in the selected period.")

    strat, hold = performance(result["strategy"]), performance(result["buy_hold"])
    trades = int(result["held"].diff().abs().sum())
    print(f"{args.ticker}: {result.index[0].date()} -> {result.index[-1].date()}  "
          f"({args.window}-day avg, ±{args.band * 100:.1f}% band, cash yield {args.cash_rate * 100:.1f}%)")
    print(f"{'':16}{'TrendBot':>12}{'Buy & hold':>12}")
    for label, attr in [("Total return", "total_return"), ("Annual return", "cagr"),
                        ("Volatility", "volatility"), ("Worst drop", "max_drawdown")]:
        print(f"{label:16}{pct(getattr(strat, attr)):>12}{pct(getattr(hold, attr)):>12}")
    print(f"{'Sharpe':16}{strat.sharpe:>12.2f}{hold.sharpe:>12.2f}")
    print(f"Switches: {trades}   Time invested: {result['held'].mean() * 100:.0f}%")
    print(f"$10,000 became ${10_000 * result['strategy'].iloc[-1]:,.0f} "
          f"(buy & hold: ${10_000 * result['buy_hold'].iloc[-1]:,.0f})")
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    sig = compute_signals(get_prices(args), args.window, args.band)
    want_in = bool(sig["position"].iloc[-1])
    broker = Alpaca(live=args.live)
    held = broker.position_qty(args.ticker)
    mode = "LIVE" if args.live else "paper"
    print(f"[{mode}] {args.ticker} signal: {'IN' if want_in else 'OUT'}; currently holding {held:g} shares")

    if want_in and held <= 0:
        cash = float(broker.account()["cash"]) * args.allocation
        if cash < 1:
            print("Signal is IN but there is no cash to invest.")
            return 0
        action = f"BUY ${cash:,.2f} of {args.ticker}"
        if args.dry_run:
            print(f"Dry run - would {action}")
        else:
            order = broker.buy_notional(args.ticker, cash)
            print(f"Placed: {action} (order {order['id']})")
    elif not want_in and held > 0:
        action = f"SELL all {held:g} shares of {args.ticker}"
        if args.dry_run:
            print(f"Dry run - would {action}")
        else:
            broker.sell_all(args.ticker)
            print(f"Placed: {action}")
    else:
        print("Already positioned correctly - nothing to do.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("ticker", nargs="?", default="SPY", help="symbol to time (default: SPY)")
    common.add_argument("--csv", help="read prices from this CSV instead of downloading")
    common.add_argument("--window", type=int, default=200, help="moving-average length in days (default: 200)")
    common.add_argument("--band", type=float, default=0.01,
                        help="buffer around the average, as a fraction (default: 0.01 = 1%%)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("signal", parents=[common], help="show today's signal").set_defaults(func=cmd_signal)

    bt = sub.add_parser("backtest", parents=[common], help="test the rule on past data")
    bt.add_argument("--start", help="first date to report, e.g. 2005-01-01")
    bt.add_argument("--cash-rate", type=float, default=0.02,
                    help="annual yield while in cash (default: 0.02)")
    bt.add_argument("--cost", type=float, default=0.0005, help="cost per switch (default: 0.0005)")
    bt.set_defaults(func=cmd_backtest)

    tr = sub.add_parser("trade", parents=[common], help="sync an Alpaca account to the signal")
    tr.add_argument("--allocation", type=float, default=1.0,
                    help="fraction of cash to invest on a buy (default: 1.0)")
    tr.add_argument("--dry-run", action="store_true", help="show what would happen without ordering")
    tr.add_argument("--live", action="store_true", help="use the LIVE account (real money)")
    tr.set_defaults(func=cmd_trade)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.ticker = args.ticker.upper()
    if args.command == "trade" and args.live:
        answer = input(f"Trade {args.ticker} with REAL money? Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
