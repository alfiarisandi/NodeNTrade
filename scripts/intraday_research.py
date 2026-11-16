"""Riset strategi intraday M15 (bias H4/H1) untuk EURUSD & XAUUSD: lima strategi + hybrid regime (bot.intraday).

  python scripts/intraday_research.py                        # semua studi
  python scripts/intraday_research.py --study strategies     # A-F berdampingan, in-sample vs out-of-sample
  python scripts/intraday_research.py --study regime         # tiap strategi per regime dan per sesi
  python scripts/intraday_research.py --study exits          # TP tetap / parsial / BE / trailing ATR, EMA20, swing, VWAP
  python scripts/intraday_research.py --study score          # hasil per kelompok skor confluence
  python scripts/intraday_research.py --study filters        # efek filter sinyal palsu
  python scripts/intraday_research.py --study walkforward    # latih 12 bulan, uji 6 bulan, bergulir
  python scripts/intraday_research.py --study m5             # entry M15 vs konfirmasi candle M5

Strategi: A EMA+ADX+price action, B Smart Money Concepts, C VWAP+EMA, D Opening Range Breakout,
E mean reversion, F hybrid (strategi dipilih sesuai regime). Hasil ke reports/intraday_*.csv dan
reports/intraday_summary.txt. Gerbang kelulusan ditetapkan sebelum melihat hasil (lihat GATE).
"""
import argparse
from dataclasses import replace

import numpy as np
import pandas as pd
from _common import setup_logging

from bot.backtest.engine import backtest_intraday, load_frames, simulate_portfolio
from bot.backtest.report import breakdown, extended_metrics, metrics
from bot.backtest.robustness import monte_carlo, r_stats, stressed, without_top
from bot.config import load_settings
from bot.intraday import STRATEGIES, ExitPolicy, IntradayParams, all_signals, hybrid, mtf_features
from bot.strategy import simulate_exit

SYMBOLS = ["EURUSD", "XAUUSD"]
NAMES = [*STRATEGIES, "F"]
BASE_POLICY = ExitPolicy()   # TP tetap 1.5R, tanpa BE / trailing
PARTIAL = dict(tp_r=0, partial_r=1.0, partial_frac=0.5)
POLICIES: dict[str, ExitPolicy] = {
    "tp1R": ExitPolicy(tp_r=1.0),
    "tp1.5R": BASE_POLICY,
    "tp2R": ExitPolicy(tp_r=2.0),
    "tp_SR": ExitPolicy(tp_r=1.5, tp_sr=True),
    "tp2R_be0.8R": ExitPolicy(tp_r=2.0, be_at_r=0.8),
    "tp2R_be1R": ExitPolicy(tp_r=2.0, be_at_r=1.0),
    "tp2R_be1R_kunci1.5R": ExitPolicy(tp_r=2.0, be_at_r=1.0, lock_at_r=1.5, lock_fraction=0.33),
    "parsial1R_tp2R": ExitPolicy(**PARTIAL, tp2_r=2.0),
    "parsial1R_trailATR": ExitPolicy(**PARTIAL, trail="atr"),
    "parsial1R_trailEMA20": ExitPolicy(**PARTIAL, trail="ema20"),
    "parsial1R_trailSwing": ExitPolicy(**PARTIAL, trail="swing"),
    "parsial1R_trailVWAP": ExitPolicy(**PARTIAL, trail="vwap"),
    "trailATR2_saja": ExitPolicy(tp_r=0, trail="atr", trail_atr_mult=2.0),
    "trailEMA20_saja": ExitPolicy(tp_r=0, trail="ema20"),
}
WF_POLICIES = ["tp1R", "tp1.5R", "tp2R", "parsial1R_tp2R", "parsial1R_trailATR"]
WF_SCORES = [0, 65, 75]
# Gerbang minimum (out-of-sample & walk-forward) dan sasaran spek
GATE = {"profit_factor": 1.2, "expectancy_R": 0.0, "max_drawdown_%": -15.0, "trades": 100}
TARGET = {"profit_factor": 1.5, "winrate_%": 55.0}
COLS = ["trades", "trades_per_day", "winrate_%", "winrate_ci95", "profit_factor", "expectancy_R",
        "exp_before_commission_R", "avg_win_R",
        "avg_loss_R", "total_R", "max_dd_R", "recovery_factor", "max_drawdown_%", "return_%", "max_losing_streak",
        "avg_hold_h", "cost_R"]


class Lab:
    """Fitur dan kandidat sinyal per simbol (dihitung sekali), plus cache hasil backtest per konfigurasi."""

    def __init__(self, settings, symbols, ip=IntradayParams()):
        self.s, self.ip, self.symbols = settings, ip, symbols
        self.data, self._cache = {}, {}
        data_dir = settings.path(settings.data_dir)
        for sym in symbols:
            frames, meta = load_frames(data_dir, sym)
            f = mtf_features(frames, ip)
            self.data[sym] = (f, all_signals(f, ip), meta, frames)
            print(f"{sym}: M15 {f['time'].iloc[0]:%Y-%m-%d} s/d {f['time'].iloc[-1]:%Y-%m-%d}, "
                  f"{len(f)} bar, kandidat {self.data[sym][1]['strat'].value_counts().sort_index().to_dict()}")

    def trades(self, name: str, pol_key: str = "tp1.5R", room_filter: bool = True, entry_delay: int = 0) -> pd.DataFrame:
        """Trade semua simbol untuk strategi A-E atau F (hybrid), satu posisi per simbol."""
        return self.custom(name, POLICIES[pol_key], self.ip, room_filter, entry_delay)

    def custom(self, name: str, pol: ExitPolicy, ip: IntradayParams, room_filter: bool = True,
               entry_delay: int = 0) -> pd.DataFrame:
        """Seperti trades(), dengan ExitPolicy dan parameter SL/filter (ip) bebas. Sinyal tetap dari self.ip.
        name "R" = pembanding acak: entry tiap 16 bar dengan arah acak (benih tetap), SL di luar 5 bar terakhir.
        entry_delay = entry terlambat N candle M15."""
        key = (name, pol, ip, room_filter, entry_delay)
        if key not in self._cache:
            out = []
            for sym in self.symbols:
                f, cands, meta, _ = self.data[sym]
                if name == "R":
                    c = random_candidates(f, sym)
                else:
                    c = hybrid(cands) if name == "F" else cands[cands["strat"] == name]
                t = backtest_intraday(sym, f, c, meta, self.s, ip, pol, room_filter, entry_delay=entry_delay)
                # Hybrid: ukuran posisi separuh saat volatilitas tinggi
                t["size"] = np.where(t["high_vol"], 0.5, 1.0) if name == "F" else 1.0
                out.append(t)
            self._cache[key] = pd.concat(out, ignore_index=True)
        return self._cache[key]

    def portfolio(self, trades: pd.DataFrame) -> tuple[dict, dict]:
        """Metrik portofolio dengan batas harian (8 trade, rugi 2%, jeda 2 rugi beruntun) + PF per pair."""
        bt, risk = self.s.backtest, self.s.risk
        taken, eq = simulate_portfolio(trades, bt.initial_balance, risk.risk_per_trade, 2, max_per_day=8,
                                       daily_loss=risk.max_daily_loss, stop_after_losses=2)
        port = extended_metrics(taken, eq, bt.initial_balance) if len(taken) else {"trades": 0}
        pairs = {s: metrics(trades[trades["symbol"] == s]).get("profit_factor") for s in self.symbols}
        return port, pairs


def random_candidates(f: pd.DataFrame, sym: str, every: int = 16) -> pd.DataFrame:
    """Kandidat pembanding tanpa edge: tiap `every` bar, arah lempar koin (benih tetap per simbol)."""
    ok = f[["ema200", "atr", "h1_atr", "h4_ema200"]].notna().all(axis=1).to_numpy()
    i = np.arange(0, len(f), every)
    i = i[ok[i]]
    d = np.random.default_rng(sum(map(ord, sym))).choice([1, -1], len(i))
    lo, hi = f["low"].rolling(5).min().to_numpy(), f["high"].rolling(5).max().to_numpy()
    return pd.DataFrame({"i": i, "dir": d, "sl_ref": np.where(d == 1, lo[i], hi[i]), "strat": "R",
                         "regime": f["regime"].to_numpy()[i]})


def period(trades: pd.DataFrame, start, end) -> pd.DataFrame:
    return trades[(trades["signal_time"] >= start) & (trades["signal_time"] < end)]


def verdict(m: dict, pairs: dict) -> str:
    """Gerbang minimum + sasaran spek untuk satu baris hasil."""
    if not m.get("trades"):
        return "GAGAL: tidak ada trade"
    fails = []
    if m["trades"] < GATE["trades"]:
        fails.append(f"trade {m['trades']} < {GATE['trades']}")
    if m["profit_factor"] <= GATE["profit_factor"]:
        fails.append(f"PF {m['profit_factor']} <= {GATE['profit_factor']}")
    if m["expectancy_R"] <= GATE["expectancy_R"]:
        fails.append("expectancy <= 0")
    if m.get("max_drawdown_%", 0) <= GATE["max_drawdown_%"]:
        fails.append(f"DD {m['max_drawdown_%']}%")
    if any(v is None or v <= 1 for v in pairs.values()):
        fails.append("ada pair PF <= 1")
    if fails:
        return "GAGAL: " + ", ".join(fails)
    target = m["profit_factor"] >= TARGET["profit_factor"] and m["winrate_%"] >= TARGET["winrate_%"]
    return "LULUS gerbang" + (" + sasaran spek" if target else " (sasaran spek PF 1.5 / win 55% belum)")


class Report:
    def __init__(self, out_dir):
        self.dir, self.lines = out_dir, []
        out_dir.mkdir(parents=True, exist_ok=True)

    def table(self, title: str, df: pd.DataFrame, file: str | None = None) -> None:
        text = f"\n=== {title} ===\n{df.to_string() if len(df) else '(kosong)'}"
        print(text)
        self.lines.append(text)
        if file:
            df.to_csv(self.dir / f"intraday_{file}.csv")

    def note(self, text: str) -> None:
        print(text)
        self.lines.append(text)

    def save(self) -> None:
        (self.dir / "intraday_summary.txt").write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def study_strategies(lab: Lab, rep: Report, split, end) -> None:
    for label, a, b in (("IN-SAMPLE", pd.Timestamp.min, split), ("OUT-OF-SAMPLE", split, end)):
        rows = {}
        for name in NAMES:
            t = period(lab.trades(name), a, b)
            m, pairs = lab.portfolio(t)
            rows[f"{name} {STRATEGIES.get(name, 'Hybrid regime')}"] = {
                **m, **{f"PF_{s}": v for s, v in pairs.items()}, "gerbang": verdict(m, pairs)}
        df = pd.DataFrame(rows).T.reindex(columns=[*COLS, *(f"PF_{s}" for s in lab.symbols), "gerbang"])
        rep.table(f"STRATEGI A-F | {label} | exit TP 1.5R | portofolio risiko "
                  f"{lab.s.risk.risk_per_trade:.1%}, maks 8 trade/hari", df, f"strategies_{label.lower()}")
    for sym in lab.symbols:   # per pair, seluruh periode, trade mentah (tanpa batas portofolio)
        rows = {}
        for name in NAMES:
            t = lab.trades(name)
            t = t[t["symbol"] == sym]
            late = lab.trades(name, entry_delay=1)
            late = late[late["symbol"] == sym]
            rows[name] = {k: {**extended_metrics(period(t, a, b)), **robust_cols(period(t, a, b)),
                              "PF_telat_1bar": r_stats(period(late, a, b)["r_net"])["pf"]}
                          for k, a, b in (("IS", pd.Timestamp.min, split), ("OOS", split, end))}
        df = pd.DataFrame({f"{n} {k}": v for n, r in rows.items() for k, v in r.items()}).T
        rep.table(f"PER PAIR {sym} | trade mentah per strategi (R bersih termasuk swap; stress biaya, telat 1 bar, "
                  f"Monte Carlo)", df.reindex(columns=COLS + ROBUST_COLS).dropna(axis=1, how="all"),
                  f"strategies_{sym}")


ROBUST_COLS = ["swap_R", "PF_biaya_x2", "PF_biaya_x3", "PF_telat_1bar", "total_R_tanpa_top3", "MC_P_total<=0_%"]


def robust_cols(t: pd.DataFrame) -> dict:
    """Kolom ketahanan satu kelompok trade: swap rata-rata, PF dengan biaya x2/x3, tanpa 3 trade terbaik, Monte Carlo."""
    if t.empty:
        return {}
    return {"swap_R": round(float(t["swap_r"].mean()), 3), "PF_biaya_x2": r_stats(stressed(t, 2))["pf"],
            "PF_biaya_x3": r_stats(stressed(t, 3))["pf"], "total_R_tanpa_top3": without_top(t["r_net"], 3)["total_R"],
            "MC_P_total<=0_%": monte_carlo(t["r_net"]).get("p_total_le_0_%")}


def study_regime(lab: Lab, rep: Report, split, end) -> None:
    allt = pd.concat([lab.trades(n) for n in STRATEGIES], ignore_index=True)
    allt["periode"] = np.where(allt["signal_time"] < split, "IS", "OOS")
    keep = ["trades", "winrate_%", "winrate_ci95", "profit_factor", "expectancy_R", "total_R"]
    for by, file in ((["strategy", "regime"], "regime"), (["strategy", "session"], "session"),
                     (["symbol", "session"], "session_pair"), (["strategy", "high_vol"], "volatility")):
        for per in ("IS", "OOS"):
            rep.table(f"{' x '.join(by).upper()} | {per} | trade mentah, exit TP 1.5R",
                      breakdown(allt[allt["periode"] == per], by).reindex(columns=keep), f"{file}_{per.lower()}")
    rep.note("Catatan: regime 'news-driven' tidak diuji karena kalender berita historis tidak tersedia.")


def study_exits(lab: Lab, rep: Report, split, end) -> None:
    keep = ["trades", "winrate_%", "profit_factor", "expectancy_R", "avg_win_R", "avg_loss_R", "total_R",
            "max_dd_R", "avg_hold_h"]
    for name in NAMES:
        rows = {}
        for key in POLICIES:
            t = lab.trades(name, key)
            is_, oos = extended_metrics(period(t, pd.Timestamp.min, split)), extended_metrics(period(t, split, end))
            rows[key] = {**{f"IS_{k}": is_.get(k) for k in keep}, **{f"OOS_{k}": oos.get(k) for k in keep}}
        df = pd.DataFrame(rows).T
        rep.table(f"EXIT | strategi {name} | trade mentah kedua pair", df.sort_values("IS_expectancy_R", ascending=False),
                  f"exits_{name}")


def study_score(lab: Lab, rep: Report, split, end) -> None:
    allt = pd.concat([lab.trades(n) for n in STRATEGIES], ignore_index=True)
    allt["skor"] = pd.cut(allt["score"], [-1, 64, 74, 84, 100], labels=["<65", "65-74", "75-84", "85-100"])
    keep = ["trades", "winrate_%", "winrate_ci95", "profit_factor", "expectancy_R", "total_R"]
    for label, a, b in (("IS", pd.Timestamp.min, split), ("OOS", split, end)):
        t = period(allt, a, b)
        rep.table(f"SKOR CONFLUENCE | {label} | semua strategi A-E, trade mentah", breakdown(t, "skor").reindex(columns=keep),
                  f"score_{label.lower()}")
        rep.table(f"SKOR x STRATEGI | {label}", breakdown(t, ["strategy", "skor"]).reindex(columns=keep),
                  f"score_strategy_{label.lower()}")
        rho = t["score"].rank().corr(t["r_net"].rank())   # Spearman = Pearson atas peringkat (tanpa scipy)
        rep.note(f"Korelasi Spearman skor vs hasil R ({label}, {len(t)} trade): {rho:+.3f}")


def study_filters(lab: Lab, rep: Report, split, end) -> None:
    loose = Lab(lab.s, lab.symbols, replace(lab.ip, max_ext_atr=99.0))   # tanpa filter jarak dari EMA/VWAP/level
    keep = ["trades", "winrate_%", "profit_factor", "expectancy_R", "total_R"]
    rows = {}
    for name in NAMES:
        for label, src, room in (("semua filter", lab, True), ("tanpa filter S/R di depan", lab, False),
                                 ("tanpa filter jarak (entry telat)", loose, True), ("tanpa keduanya", loose, False)):
            t = src.trades(name, room_filter=room)
            is_, oos = extended_metrics(period(t, pd.Timestamp.min, split)), extended_metrics(period(t, split, end))
            rows[f"{name} | {label}"] = {**{f"IS_{k}": is_.get(k) for k in keep},
                                         **{f"OOS_{k}": oos.get(k) for k in keep}}
    rep.table("FILTER SINYAL PALSU | trade mentah kedua pair, exit TP 1.5R", pd.DataFrame(rows).T, "filters")


def study_walkforward(lab: Lab, rep: Report, split, end, train_months=12, test_months=6, min_train=40) -> None:
    """Tiap jendela: pilih (strategi, exit, skor minimum) dengan expectancy terbaik di 12 bulan latih, nilai hanya
    di 6 bulan uji berikutnya. Tidak ada konfigurasi dengan expectancy > 0 di jendela latih = tidak trade."""
    configs = {(n, p, sc): lab.trades(n, p)[lambda t, sc=sc: t["score"] >= sc]
               for n in NAMES for p in WF_POLICIES for sc in WF_SCORES}
    first = min(f["time"].iloc[0] for f, *_ in lab.data.values()) + pd.DateOffset(months=2)   # 2 bulan warm-up
    end = max(f["time"].iloc[-1] for f, *_ in lab.data.values())
    folds, chosen_trades, fixed = [], [], {n: [] for n in NAMES}
    start = first
    while start + pd.DateOffset(months=train_months) < end:
        mid = start + pd.DateOffset(months=train_months)
        stop = min(mid + pd.DateOffset(months=test_months), end)
        best, best_exp = None, 0.0
        for key, t in configs.items():
            tr = period(t, start, mid)
            if len(tr) >= min_train and tr["r_net"].mean() > best_exp:
                best, best_exp = key, tr["r_net"].mean()
        test = period(configs[best], mid, stop) if best else configs[next(iter(configs))].iloc[0:0]
        m = metrics(test)
        folds.append({"latih": f"{start:%Y-%m}..{mid:%Y-%m}", "uji": f"{mid:%Y-%m}..{stop:%Y-%m}",
                      "terpilih": "/".join(map(str, best)) if best else "TIDAK TRADE",
                      "exp_latih_R": round(best_exp, 3), "trade_uji": m.get("trades", 0),
                      "PF_uji": m.get("profit_factor"), "exp_uji_R": m.get("expectancy_R"),
                      "total_R_uji": m.get("total_R", 0.0)})
        chosen_trades.append(test)
        for n in NAMES:
            fixed[n].append(period(lab.trades(n), mid, stop))
        start += pd.DateOffset(months=test_months)
    df = pd.DataFrame(folds)
    rep.table("WALK-FORWARD | per jendela (latih 12 bulan -> uji 6 bulan)", df, "walkforward_folds")
    rows = {}
    wf = pd.concat(chosen_trades, ignore_index=True)
    m, pairs = lab.portfolio(wf)
    positive = int((df["total_R_uji"] > 0).sum())
    rows["Walk-forward (parameter dipilih ulang)"] = {
        **m, **{f"PF_{s}": v for s, v in pairs.items()}, "jendela_positif": f"{positive}/{len(df)}",
        "gerbang": verdict(m, pairs) + ("" if positive * 2 > len(df) else ", mayoritas jendela tidak positif")}
    for n in NAMES:
        t = pd.concat(fixed[n], ignore_index=True)
        m, pairs = lab.portfolio(t)
        pos = sum(1 for part in fixed[n] if part["r_net"].sum() > 0)
        rows[f"{n} parameter tetap (TP 1.5R)"] = {**m, **{f"PF_{s}": v for s, v in pairs.items()},
                                                 "jendela_positif": f"{pos}/{len(df)}", "gerbang": verdict(m, pairs)}
    out = pd.DataFrame(rows).T.reindex(columns=[*COLS, *(f"PF_{s}" for s in lab.symbols), "jendela_positif", "gerbang"])
    rep.table("WALK-FORWARD | gabungan semua jendela uji (portofolio)", out, "walkforward")


def study_m5(lab: Lab, rep: Report, split, end, wait_bars=6, tp_r=1.5) -> None:
    """Di periode yang punya data M5: bandingkan entry langsung di open bar setelah sinyal M15 dengan entry yang
    menunggu konfirmasi candle M5 (close searah di atas high / di bawah low bar M5 sebelumnya, maks 30 menit).
    Keduanya disimulasikan di bar M5 dengan SL yang sama dan TP 1.5R."""
    bt = lab.s.backtest
    rows = {}
    for name in NAMES:
        res = {"M15 langsung": [], "konfirmasi M5": []}
        for sym in lab.symbols:
            f, _, meta, frames = lab.data[sym]
            if "M5" not in frames:
                continue
            m5 = frames["M5"].reset_index(drop=True)
            o, h, l, c = (m5[k].to_numpy(float) for k in ("open", "high", "low", "close"))
            sp = np.where(m5["spread"] > 0, m5["spread"], m5["spread"][m5["spread"] > 0].median()) * meta["point"]
            slip = bt.slippage_points * meta["point"]
            commission = bt.commission_per_lot * meta["tick_size"] / meta["tick_value"]
            times = m5["time"].to_numpy()
            t = lab.trades(name)
            t = t[(t["symbol"] == sym) & (t["entry_time"] >= m5["time"].iloc[0])]
            for row in t.itertuples():
                d = row.direction
                k0 = int(np.searchsorted(times, np.datetime64(row.entry_time)))
                if k0 + wait_bars + 2 >= len(m5):
                    continue
                confirm = next((k for k in range(k0, k0 + wait_bars)
                                if d * (c[k] - o[k]) > 0 and (c[k] > h[k - 1] if d == 1 else c[k] < l[k - 1])), None)
                for label, e in (("M15 langsung", k0), ("konfirmasi M5", None if confirm is None else confirm + 1)):
                    if e is None:
                        continue
                    entry = o[e] + sp[e] + slip if d == 1 else o[e] - slip
                    dist = d * (entry - row.sl)
                    # harga sudah terlalu dekat SL: biaya jauh melebihi batas (toleransi 20% karena SL trade
                    # M15 sering tepat di batas biaya dan spread bar M5 sedikit berbeda)
                    if dist <= 0.8 * (sp[e] + 2 * slip + commission) / lab.ip.max_cost_r:
                        continue
                    out = simulate_exit(o, h, l, sp, e, d, entry, row.sl, entry + d * tp_r * dist, close=c,
                                        max_bars=3 * BASE_POLICY.max_hold_bars)
                    px = (c[-1] if out is None else out[1]) - d * slip
                    j = len(m5) - 1 if out is None else out[0]
                    res[label].append({"entry_time": m5["time"].iloc[e], "exit_time": m5["time"].iloc[j],
                                       "r_net": d * (px - entry) / dist - commission / dist, "cost_r": commission / dist})
        for label, lst in res.items():
            rows[f"{name} | {label}"] = extended_metrics(pd.DataFrame(lst)) if lst else {"trades": 0}
    keep = ["trades", "winrate_%", "winrate_ci95", "profit_factor", "expectancy_R", "total_R", "avg_hold_h"]
    rep.table("ENTRY M15 vs KONFIRMASI M5 | hanya periode yang punya data M5, TP 1.5R, trade mentah",
              pd.DataFrame(rows).T.reindex(columns=keep), "m5")


WR_TARGET = 80.0
WR_TPS = [0.2, 0.3, 0.5, 0.75, 1.0]
WR_STOPS = {"SL 0.5-2 ATR": (0.5, 2.0), "SL 1-3 ATR": (1.0, 3.0), "SL 1.5-4 ATR": (1.5, 4.0), "SL 2-5 ATR": (2.0, 5.0)}
WR_SESSIONS = {"semua sesi": None, "London+NY": ("London", "Overlap", "NewYork")}


def study_winrate(lab: Lab, rep: Report, split, end, min_trades=100) -> None:
    """Cari konfigurasi day trade dengan win rate >= 80%: TP dekat (0.2R-1R) x SL lebar (jarak minimum 0.5-2 ATR H1)
    x strategi A-F x sesi, dibanding entry acak (R). Win rate tinggi dari TP kecil / SL lebar bukan edge: yang dicari
    adalah win rate >= 80% DAN expectancy > 0 setelah biaya, dipilih di in-sample lalu dicek di out-of-sample.
    Win rate impas = rata-rata kalah / (rata-rata menang + rata-rata kalah)."""
    def stats(t: pd.DataFrame) -> dict:
        m = extended_metrics(t)
        if not m.get("trades"):
            return {"trades": 0}
        w, l = m["avg_win_R"], -m["avg_loss_R"]
        return {"trades": m["trades"], "win_%": m["winrate_%"], "ci95": m["winrate_ci95"],
                "impas_%": round(float(100 * l / (w + l)), 1) if w + l > 0 else None, "PF": float(m["profit_factor"]),
                "exp_R": float(m["expectancy_R"]), "total_R": float(m["total_R"]), "dd_R": m["max_dd_R"],
                "kalah_beruntun": m["max_losing_streak"]}

    configs, rows = {}, {}
    for name in [*NAMES, "R"]:
        for stop_label, (lo, hi) in WR_STOPS.items():
            for tp in WR_TPS:
                # S/R di depan cukup sejauh TP; filter jarak tetap dari sinyal
                ip = replace(lab.ip, sl_min_atr_h1=lo, sl_max_atr_h1=hi, room_r=tp)
                t = lab.custom(name, ExitPolicy(tp_r=tp), ip)
                for ses_label, sessions in WR_SESSIONS.items():
                    sub = t if sessions is None else t[t["session"].isin(sessions)]
                    key = f"{name} | TP {tp:g}R | {stop_label} | {ses_label}"
                    configs[key] = sub
                    is_, oos = stats(period(sub, pd.Timestamp.min, split)), stats(period(sub, split, end))
                    rows[key] = {**{f"IS_{k}": v for k, v in is_.items()}, **{f"OOS_{k}": v for k, v in oos.items()}}
    df = pd.DataFrame(rows).T
    df.to_csv(rep.dir / "intraday_winrate_all.csv")
    num = df.apply(pd.to_numeric, errors="coerce")
    rep.note(f"\nWIN RATE >= {WR_TARGET:g}%: {len(df)} konfigurasi diuji (strategi A-F + acak R, TP {WR_TPS} R, "
             f"{len(WR_STOPS)} lebar SL, 2 filter sesi). Tabel lengkap: intraday_winrate_all.csv")

    # Pembanding acak: win rate yang didapat tanpa edge sama sekali
    rnd = df[df.index.str.startswith("R |") & df.index.str.endswith("semua sesi")]
    rep.table("PEMBANDING ENTRY ACAK (tanpa edge): win rate hanya dari geometri TP/SL",
              rnd[["IS_trades", "IS_win_%", "IS_impas_%", "IS_PF", "IS_exp_R", "OOS_win_%", "OOS_impas_%", "OOS_PF",
                   "OOS_exp_R"]], "winrate_random")

    real = ~df.index.str.startswith("R |")
    enough = (num["IS_trades"] >= min_trades) & (num["OOS_trades"] >= min_trades)
    high = df[real & enough & (num["IS_win_%"] >= WR_TARGET)]
    rep.table(f"IN-SAMPLE WIN RATE >= {WR_TARGET:g}% (>= {min_trades} trade di tiap periode), urut expectancy in-sample",
              high.sort_values("IS_exp_R", ascending=False).head(25), "winrate_high")
    best = df[real & enough].sort_values("IS_exp_R", ascending=False).head(15)
    rep.table("15 KONFIGURASI DENGAN EXPECTANCY IN-SAMPLE TERBAIK (berapa pun win rate-nya)", best, "winrate_best_exp")
    passed = df[real & enough & (num["IS_win_%"] >= WR_TARGET) & (num["IS_exp_R"] > 0)
                & (num["OOS_win_%"] >= WR_TARGET) & (num["OOS_exp_R"] > 0) & (num["OOS_PF"] > GATE["profit_factor"])]
    rep.note(f"Lolos (win rate >= {WR_TARGET:g}% dan expectancy > 0 di KEDUA periode, PF out-of-sample > "
             f"{GATE['profit_factor']}): {len(passed)} dari {int(real.sum())} konfigurasi."
             + ("" if passed.empty else "\n" + passed.to_string()))
    both_pos = df[real & enough & (num["IS_exp_R"] > 0) & (num["OOS_exp_R"] > 0)]
    rep.table("EXPECTANCY > 0 DI KEDUA PERIODE (win rate berapa pun)", both_pos.sort_values("OOS_exp_R", ascending=False),
              "winrate_both_positive")

    # Walk-forward: tiap jendela pilih konfigurasi win rate >= 80% dengan expectancy terbaik di 12 bulan latih
    first = min(f["time"].iloc[0] for f, *_ in lab.data.values()) + pd.DateOffset(months=2)
    last = max(f["time"].iloc[-1] for f, *_ in lab.data.values())
    folds, picked, start = [], [], first
    real_cfg = {k: v for k, v in configs.items() if not k.startswith("R |")}
    while start + pd.DateOffset(months=12) < last:
        mid = start + pd.DateOffset(months=12)
        stop = min(mid + pd.DateOffset(months=6), last)
        best_key, best_exp = None, 0.0
        for key, t in real_cfg.items():
            tr = period(t, start, mid)
            if len(tr) >= 40 and (tr["r_net"] > 0).mean() * 100 >= WR_TARGET and tr["r_net"].mean() > best_exp:
                best_key, best_exp = key, tr["r_net"].mean()
        test = period(real_cfg[best_key], mid, stop) if best_key else next(iter(real_cfg.values())).iloc[0:0]
        s = stats(test)
        folds.append({"uji": f"{mid:%Y-%m}..{stop:%Y-%m}", "terpilih": best_key or "TIDAK ADA (win >= 80% & exp > 0)",
                      "exp_latih_R": round(best_exp, 3), **{k: s.get(k) for k in ("trades", "win_%", "PF", "exp_R", "total_R")}})
        picked.append(test)
        start += pd.DateOffset(months=6)
    rep.table(f"WALK-FORWARD WIN RATE >= {WR_TARGET:g}% | pilih di 12 bulan latih, uji 6 bulan berikutnya",
              pd.DataFrame(folds), "winrate_walkforward")
    allp = pd.concat(picked, ignore_index=True)
    rep.note(f"Walk-forward gabungan: {stats(allp) if len(allp) else 'tidak ada trade'}")


STUDIES = {"winrate": study_winrate, "strategies": study_strategies, "regime": study_regime, "exits": study_exits, "score": study_score,
           "filters": study_filters, "walkforward": study_walkforward, "m5": study_m5}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", choices=[*STUDIES, "all"], default="all")
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--split", default="2025-01-01", help="batas in-sample / out-of-sample")
    args = ap.parse_args()
    setup_logging()

    s = load_settings()
    lab = Lab(s, args.symbols)
    rep = Report(s.path(s.reports_dir))
    split, end = pd.Timestamp(args.split), pd.Timestamp.max
    rep.note(f"Split in-sample / out-of-sample: {split:%Y-%m-%d}. Biaya: spread per bar + slippage "
             f"{s.backtest.slippage_points:g} point per sisi + komisi ${s.backtest.commission_per_lot:g}/lot. "
             f"Gerbang minimum: PF > {GATE['profit_factor']}, expectancy > 0, DD < 15%, >= {GATE['trades']} trade, "
             f"kedua pair PF > 1. Sasaran spek: PF >= {TARGET['profit_factor']}, win rate >= {TARGET['winrate_%']:g}%.")
    for name, fn in STUDIES.items():
        if args.study in (name, "all"):
            fn(lab, rep, split, end)
    if args.study == "all":
        rep.save()


if __name__ == "__main__":
    main()
