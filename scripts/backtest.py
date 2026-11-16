"""Backtest lapisan teknikal dari data CSV (hasil download_data.py).

Contoh: python scripts/backtest.py --symbols EURUSD --from 2019-01-01 --to 2025-12-31 --split 2024-01-01
        python scripts/backtest.py --config config/settings.small.yaml --balance 100   # akun kecil, lot nyata
        python scripts/backtest.py --robust   # + uji ketahanan per pair (swap, biaya x2/x3, telat 1 bar, MC)
R bersih termasuk swap bila <SYMBOL>_meta.json memuat data swap (download_data.py --meta-only).
"""
import argparse
import json

import pandas as pd
from _common import setup_logging

from bot.backtest.engine import run_all, simulate_account, simulate_portfolio
from bot.backtest.report import metrics, plot_equity, print_table
from bot.backtest.robustness import MC_RUNS, criteria, monte_carlo, r_stats, stressed, without_top
from bot.config import load_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--from", dest="start", default="2019-01-01")
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--split", default="2024-01-01", help="awal periode out-of-sample")
    ap.add_argument("--config", help="file settings (default: BOT_CONFIG / config/settings.yaml)")
    ap.add_argument("--balance", type=float, help="simulasi akun dengan lot nyata (lot minimum broker)")
    ap.add_argument("--robust", action="store_true",
                    help="uji ketahanan per pair: biaya x2/x3, entry telat 1 bar, sensitivitas SL, Monte Carlo, "
                         "tanpa trade terbaik, kriteria OOS (reports/backtest_robustness.txt)")
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
    if args.robust:
        text = "\n".join(robust_report(s, names, data_dir, trades, start, end, split))
        print(text)
        (rep_dir / "backtest_robustness.txt").write_text(text, encoding="utf-8")
        print(f"Laporan ketahanan: {rep_dir / 'backtest_robustness.txt'}")


SL_SENSITIVITY = (0.6, 0.8, 1.0, 1.2, 1.4)   # x sl_atr_mult setelan; hanya dilaporkan, setelan tidak diubah


def robust_report(s, names, data_dir, trades, start, end, split) -> list[str]:
    """Ketahanan hasil per pair (R bersih termasuk swap): IS vs OOS, biaya x2/x3, entry telat 1 bar,
    tanpa trade terbaik, Monte Carlo, sensitivitas jarak SL, dan kriteria OOS. Parameter strategi tidak diubah."""
    def window(t):
        return t[(t["signal_time"] >= start) & (t["signal_time"] <= end)]

    def fmt(st):
        pf = f"{st['pf']:.2f}" if st["pf"] is not None else "-"
        return f"n {st['n']:>3} | win {st['winrate_%']}% | PF {pf} | exp {st['exp_R']:+.3f}R | total {st['total_R']:+.1f}R " \
               f"| DD {st['max_dd_R']:.1f}R | rugi beruntun {st['max_loss_streak']}" if st["n"] else "n 0"

    delayed = window(run_all(s, names, data_dir, entry_delay=1))
    sens = {}
    for k in SL_SENSITIVITY:
        v = s.model_copy(update={"strategy": s.strategy.model_copy(update={"sl_atr_mult": s.strategy.sl_atr_mult * k})})
        sens[k] = window(run_all(v, names, data_dir))
    out = [f"\n##### UJI KETAHANAN (R bersih: spread, slippage, komisi, swap; OOS mulai {split:%Y-%m-%d}) #####"]
    for name in names:
        out.append(f"\n=== {name} ===")
        for label, oos in (("IS ", False), ("OOS", True)):
            def part(t):
                t = t[t["symbol"] == name]
                return t[(t["signal_time"] >= split) == oos]
            t = part(trades)
            if t.empty:
                continue
            out.append(f"{label} dasar        : {fmt(r_stats(t['r_net']))} | swap rata-rata {t['swap_r'].mean():+.3f}R")
            for m in (2, 3):
                out.append(f"{label} biaya x{m}      : {fmt(r_stats(stressed(t, m)))}")
            out.append(f"{label} entry telat 1 : {fmt(r_stats(part(delayed)['r_net']))}")
            for k in (1, 3):
                out.append(f"{label} tanpa top {k}   : {fmt(without_top(t['r_net'], k))}")
        allr = trades[trades["symbol"] == name]["r_net"]
        mc = monte_carlo(allr)
        if mc:
            out.append(f"Monte Carlo {len(allr)} trade x{MC_RUNS}: P(total <= 0) {mc['p_total_le_0_%']}% | total p5 "
                       f"{mc['total_p5']:+.1f}R, median {mc['total_median']:+.1f}R | DD median {mc['dd_median_R']}R, "
                       f"p95 {mc['dd_p95_R']}R")
        out.append(f"Sensitivitas jarak SL (setelan {s.strategy.sl_atr_mult:g} ATR), PF IS / OOS:")
        for k, t in sens.items():
            t = t[t["symbol"] == name]
            is_, oos_ = r_stats(t[t["signal_time"] < split]["r_net"]), r_stats(t[t["signal_time"] >= split]["r_net"])
            mark = " <- setelan" if k == 1.0 else ""
            out.append(f"  SL {s.strategy.sl_atr_mult * k:.2f} ATR: PF {is_['pf']} / {oos_['pf']} | total "
                       f"{is_['total_R']:+.1f}R / {oos_['total_R']:+.1f}R{mark}")
        oos_t = trades[(trades["symbol"] == name) & (trades["signal_time"] >= split)]
        if len(oos_t):
            cr = criteria(oos_t)
            status = "LULUS" if cr["pass"] else "TIDAK LULUS"
            out.append(f"Kriteria OOS: {status} | " + "; ".join(f"{k}: {'ya' if v else 'TIDAK'}"
                                                                for k, v in cr["checks"].items()))
    out.append("\nCatatan: swap memakai tarif broker saat data meta diunduh untuk seluruh histori (perkiraan); data "
               "OHLC tanpa tick; spread bar = spread minimum bar.")
    return out


if __name__ == "__main__":
    main()
