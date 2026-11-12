"""Backtest lapisan teknikal dari data CSV (hasil download_data.py).

Contoh: python scripts/backtest.py --symbols EURUSD --from 2019-01-01 --to 2025-12-31 --split 2024-01-01
        python scripts/backtest.py --config config/settings.small.yaml --balance 100   # akun kecil, lot nyata
"""
import argparse
import json

import pandas as pd
from _common import setup_logging

from bot.backtest.engine import run_all, simulate_account, simulate_portfolio
from bot.backtest.report import metrics, plot_equity, print_table
from bot.config import load_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--from", dest="start", default="2019-01-01")
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--split", default="2024-01-01", help="awal periode out-of-sample")
    ap.add_argument("--config", help="file settings (default: BOT_CONFIG / config/settings.yaml)")
    ap.add_argument("--balance", type=float, help="simulasi akun dengan lot nyata (lot minimum broker)")
    args = ap.parse_args()
    setup_logging()

    s = load_settings(args.config)
    data_dir, rep_dir = s.path(s.data_dir), s.path(s.reports_dir)
    names = args.symbols or [x.name for x in s.symbols]
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.max
    split = pd.Timestamp(args.split)
    bt, risk = s.backtest, s.risk
    initial = args.balance or bt.initial_balance
    metas = {n: json.loads((data_dir / f"{n}_meta.json").read_text(encoding="utf-8")) for n in names}

    def simulate(tp, max_open):
        if args.balance:   # lot nyata: lot minimum dipakai bila risikonya <= min_lot_max_risk
            return simulate_account(tp, metas, initial, risk.risk_per_trade,
                                    max(risk.risk_per_trade, risk.min_lot_max_risk), max_open)
        return simulate_portfolio(tp, initial, risk.risk_per_trade, max_open, max_per_day=risk.max_trades_per_day)

    trades = run_all(s, names, data_dir)
    trades = trades[(trades["signal_time"] >= start) & (trades["signal_time"] <= end)]
    if trades.empty:
        print("Tidak ada trade")
        return

    periods = {
        "in-sample": trades[trades["signal_time"] < split],
        "out-of-sample": trades[trades["signal_time"] >= split],
        "semua": trades,
    }
    rep_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    oos = {}
    for label, tp in periods.items():
        if tp.empty:
            continue
        rows = {}
        for name in names:
            sub = tp[tp["symbol"] == name]
            if sub.empty:
                continue
            taken, eq = simulate(sub, 1)
            rows[name] = metrics(taken, eq, initial)
        taken, eq = simulate(tp, risk.max_open_positions)
        rows["PORTOFOLIO"] = metrics(taken, eq, initial)
        if label == "out-of-sample":
            oos = rows["PORTOFOLIO"]
        header = f"\n=== {label.upper()} ({tp['signal_time'].min():%Y-%m-%d} s/d {tp['signal_time'].max():%Y-%m-%d}) ==="
        print(header)
        lines += [header, print_table(rows)]
        png = plot_equity(eq, rep_dir / f"equity_{label}.png", f"Equity portofolio - {label}")
        print(f"Grafik: {png}")

    trades.to_csv(rep_dir / "backtest_trades.csv", index=False)
    if oos:
        ok = (oos.get("profit_factor", 0) > 1.2 and oos.get("max_drawdown_%", -100) > -15
              and oos.get("trades", 0) >= 100)
        verdict = ("LULUS kriteria demo (PF>1.2, DD<15%, >=100 trade)" if ok else
                   "BELUM LULUS kriteria demo (PF>1.2, DD<15%, >=100 trade)")
        print(f"\nOut-of-sample: {verdict}")
        lines.append(f"\nOut-of-sample: {verdict}")
    (rep_dir / "backtest_summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Detail trade: {rep_dir / 'backtest_trades.csv'}")


if __name__ == "__main__":
    main()
