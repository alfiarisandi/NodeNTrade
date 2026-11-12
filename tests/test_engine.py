from bot.backtest.engine import backtest_symbol, simulate_portfolio, to_frame
from bot.backtest.report import metrics


def test_backtest_runs_and_costs_applied(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    trades = to_frame(backtest_symbol("EURUSD", h1, h4, eurusd_meta, settings, 0))
    assert len(trades) > 0
    assert (trades["cost_r"] > 0).all()
    assert (trades["r_net"] < trades["r_gross"]).all()
    # satu posisi per simbol: tidak ada trade yang tumpang tindih
    t = trades.sort_values("entry_time")
    assert (t["entry_time"].iloc[1:].values >= t["exit_time"].iloc[:-1].values).all()


def test_portfolio_and_metrics(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    trades = to_frame(backtest_symbol("EURUSD", h1, h4, eurusd_meta, settings, 0))
    taken, eq = simulate_portfolio(trades, 10_000, 0.01, 3)
    m = metrics(taken, eq, 10_000)
    assert m["trades"] == len(taken)
    assert abs(eq.iloc[-1] - 10_000 - taken["pnl"].sum()) < 1e-6
    assert m["max_drawdown_%"] <= 0


def test_spread_filter_blocks_entries(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    h1 = h1.assign(spread=50)  # di atas max_spread_points 20
    assert backtest_symbol("EURUSD", h1, h4, eurusd_meta, settings, 0) == []


def test_simulate_account_min_lot_and_risk_cap():
    import pandas as pd

    from bot.backtest.engine import simulate_account

    meta = {"tick_size": 1e-5, "tick_value": 1.0, "volume_step": 0.01, "volume_min": 0.01, "volume_max": 50}
    t0 = pd.Timestamp("2026-01-05 08:00")
    rows = [  # SL 5 pip = $0.50 di 0.01 lot (0.5% dari $100) | SL 30 pip = $3 (3%)
        {"symbol": "EURUSD", "entry_time": t0, "exit_time": t0 + pd.Timedelta(hours=1),
         "entry": 1.1, "sl": 1.0995, "r_net": 1.0},
        {"symbol": "EURUSD", "entry_time": t0 + pd.Timedelta(hours=2), "exit_time": t0 + pd.Timedelta(hours=3),
         "entry": 1.1, "sl": 1.097, "r_net": -1.0},
    ]
    taken, eq = simulate_account(pd.DataFrame(rows), {"EURUSD": meta}, 100, 0.0025, 0.01, 3)
    assert len(taken) == 1 and taken["lot"].iloc[0] == 0.01      # dipaksa lot min (0.5% <= 1%)
    assert abs(eq.iloc[-1] - 100.5) < 1e-9                       # trade kedua (3%) dilewati


def test_portfolio_max_trades_per_day():
    import pandas as pd
    from bot.backtest.engine import simulate_portfolio

    entries = pd.to_datetime(["2024-01-01 08:00", "2024-01-01 09:00", "2024-01-01 10:00", "2024-01-02 08:00"])
    t = pd.DataFrame({"symbol": ["EURUSD", "XAUUSD", "EURUSD", "EURUSD"], "entry_time": entries,
                      "exit_time": entries + pd.Timedelta(minutes=30), "r_net": [1.0, 1.0, 1.0, 1.0]})
    assert len(simulate_portfolio(t, 1000, 0.01, 5)[0]) == 4
    taken, _ = simulate_portfolio(t, 1000, 0.01, 5, max_per_day=2)
    assert list(taken["entry_time"]) == [entries[0], entries[1], entries[3]]
