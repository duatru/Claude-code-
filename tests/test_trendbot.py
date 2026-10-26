import numpy as np
import pandas as pd
import pytest

import trendbot


def series(values):
    return pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)), dtype=float)


def test_uptrend_is_in_and_downtrend_is_out():
    up = series(np.linspace(100, 200, 300))
    assert trendbot.compute_signals(up, window=50).position.iloc[-1] == 1
    down = series(np.linspace(200, 100, 300))
    assert trendbot.compute_signals(down, window=50).position.iloc[-1] == 0


def test_band_keeps_previous_state():
    closes = series([100.0] * 10 + [110.0] * 5 + [100.5] * 3)
    sig = trendbot.compute_signals(closes, window=10, band=0.05)
    # Price rises well above the average -> IN; then drifts back inside the band -> still IN.
    assert sig.position.iloc[11] == 1
    assert sig.position.iloc[-1] == 1


def test_backtest_acts_next_day_and_sits_in_cash_when_out():
    closes = series(np.r_[np.linspace(100, 150, 60), np.linspace(150, 60, 60)])
    sig = trendbot.compute_signals(closes, window=20, band=0.0)
    res = trendbot.backtest(sig, cash_rate=0.0, cost=0.0)
    # Held position is yesterday's signal.
    assert (res.held.values[1:] == sig.dropna(subset=["sma"]).position.values[:-1]).all()
    # The strategy exits the crash and beats buy-and-hold.
    assert res.strategy.iloc[-1] > res.buy_hold.iloc[-1]
    assert trendbot.performance(res.strategy).max_drawdown > trendbot.performance(res.buy_hold).max_drawdown


def test_too_little_data_raises():
    with pytest.raises(ValueError):
        trendbot.compute_signals(series([1.0] * 10), window=200)


def test_cli_with_csv(tmp_path, capsys):
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, 1500)))
    path = tmp_path / "prices.csv"
    pd.DataFrame({"Date": pd.bdate_range("2015-01-01", periods=1500), "Close": closes}).to_csv(path, index=False)
    assert trendbot.main(["signal", "TEST", "--csv", str(path)]) == 0
    assert "signal:" in capsys.readouterr().out
    assert trendbot.main(["backtest", "TEST", "--csv", str(path), "--start", "2017-01-01"]) == 0
    assert "TrendBot" in capsys.readouterr().out
