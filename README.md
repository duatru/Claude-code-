# TrendBot

A simple market-timing bot: **invested when the market is trending up, in cash when it's trending down.**

## The rule

- **IN** when the price closes more than 1% above its 200-day moving average.
- **OUT** (sell, hold cash) when it closes more than 1% below.
- In between, do nothing. That buffer stops the bot flipping back and forth when the price hovers near the average.

The bot only looks at closing prices, so you run it once a day after the market closes.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python trendbot.py signal SPY          # today's IN/OUT signal
python trendbot.py backtest SPY        # how the rule did historically vs. buy-and-hold
python trendbot.py backtest QQQ --start 2007-01-01 --window 150
```

Prices are downloaded from Yahoo Finance. You can also pass `--csv prices.csv` (columns `Date`, `Close`).

### Placing trades automatically (Alpaca)

1. Create a free account at [alpaca.markets](https://alpaca.markets) and get **paper-trading** API keys.
2. Set them:
   ```bash
   export ALPACA_API_KEY=...
   export ALPACA_SECRET_KEY=...
   ```
3. Run:
   ```bash
   python trendbot.py trade SPY --dry-run   # show what it would do
   python trendbot.py trade SPY             # place the order on your PAPER account
   ```

When the signal is IN and you have no shares, it buys with your available cash (`--allocation 0.5` uses half). When the signal is OUT and you hold shares, it sells them all. Otherwise it does nothing.

Real money needs `--live`, and the bot asks you to type `yes` first. Paper trade for a few months before you use `--live`.

To run it every weekday after the close, add a cron job like this (4:30pm ET):

```
30 16 * * 1-5  cd /path/to/repo && python trendbot.py trade SPY >> trendbot.log 2>&1
```

## Before you trust it with money

- **It's always late.** A moving average lags, so the bot sells after the market has already dropped and buys after it has already recovered. It doesn't predict anything. It's meant to step aside during long, deep declines like 2000–02 or 2008.
- **Choppy markets cost money.** In sideways markets it can sell low and buy back higher several times in a row.
- **It often makes less than buy-and-hold** in strong bull markets. You mostly get smaller worst-case losses, not bigger gains.
- **Taxes.** In a taxable account every sale can be a taxable gain. An IRA avoids that.
- Past backtests don't guarantee future results. This is a tool, not financial advice.

## Tests

```bash
pip install pytest && python -m pytest -q tests
```
