"""Riset intraday multi-strategi di M15 dengan bias H4/H1: fitur multi-timeframe, struktur pasar, regime, dan
lima generator sinyal (A EMA+ADX, B Smart Money Concepts, C VWAP+EMA, D Opening Range Breakout, E mean reversion)
plus hybrid F yang memilih strategi sesuai regime. Fungsi murni (tanpa MT5), dipakai backtest dan snapshot AI.

Semua kolom di bar ke-i hanya memakai bar yang sudah close sampai bar i (tanpa look-ahead): bar H1/H4 baru
terlihat setelah close, pivot baru terlihat setelah `pivot` bar konfirmasi, level range baru terlihat setelah
jam range selesai.
"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.config import StrategyCfg
from bot.indicators import session_vwap
from bot.strategy import TF_DELTA, add_indicators, candle_flags

NY, LONDON = ZoneInfo("America/New_York"), ZoneInfo("Europe/London")
BASE = StrategyCfg()   # EMA 20/50/200, ADX 14, ATR 14, RSI 14, Bollinger (20, 2)
STRATEGIES = {"A": "EMA + ADX + price action", "B": "Smart Money Concepts", "C": "VWAP + EMA",
              "D": "Opening Range Breakout", "E": "Mean reversion"}
# Bobot skor confluence (total 100); dipakai juga oleh skor setup AI di bot.ai.analysis
SCORE_WEIGHTS = {"trend": 20, "ema": 10, "momentum": 10, "structure": 15, "liquidity": 15, "price_action": 15,
                 "location": 10, "rr": 5}


@dataclass(frozen=True)
class IntradayParams:
    adx_trend: float = 22.0       # ADX H1 >= ini + EMA20/50/200 H1 sejajar = TREND
    adx_range: float = 18.0       # ADX H1 < ini = RANGE
    atr_pct_window: int = 960     # jendela persentil ATR M15 (960 bar = 10 hari)
    high_vol_pct: float = 0.9     # persentil ATR >= ini = HIGH_VOL (ukuran posisi dikurangi)
    pivot: int = 3                # bar kiri/kanan pivot fractal
    sl_buffer_atr: float = 0.3    # buffer SL di luar swing, x ATR M15
    sl_min_atr_h1: float = 0.5    # jarak SL minimum, x ATR H1
    sl_max_atr_h1: float = 2.0    # jarak SL maksimum, x ATR H1 (lebih lebar = sinyal dilewati)
    max_cost_r: float = 0.15      # spread + komisi maksimal 15% dari R: SL dilebarkan sampai terpenuhi
    adx_min_a: float = 20.0       # strategi A: ADX H1 minimum
    max_ext_atr: float = 1.5      # filter: entry tidak lebih jauh dari ini (x ATR M15) dari EMA20 / VWAP / level
    room_r: float = 1.0           # filter: support/resistance besar di depan entry minimal sejauh ini (x R)
    disp_atr: float = 1.2         # displacement: body candle >= ini x ATR M15
    sweep_valid_bars: int = 24    # strategi B: sweep berlaku selama N bar menunggu BOS
    zone_valid_bars: int = 16     # strategi B: FVG / order block berlaku N bar menunggu retrace
    retest_bars: int = 12         # strategi D: retest paling lama N bar setelah breakout
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    vwap_min_bars: int = 8        # strategi C: VWAP baru dipakai setelah N bar dari awal sesi


@dataclass(frozen=True)
class ExitPolicy:
    tp_r: float = 1.5             # TP tetap x R (0 = tanpa TP tetap)
    tp_sr: bool = False           # TP di support/resistance terdekat bila jaraknya 1R-3R (selain itu pakai tp_r)
    be_at_r: float = 0.0          # SL ke entry saat profit >= N x R
    lock_at_r: float = 0.0        # kunci lock_fraction dari profit maksimum setelah profit >= N x R
    lock_fraction: float = 0.0
    partial_r: float = 0.0        # tutup partial_frac di N x R, sisa lanjut (SL ke breakeven + biaya)
    partial_frac: float = 0.5
    tp2_r: float = 0.0            # TP sisa posisi (0 = tanpa)
    trail: str = "none"           # none | atr | swing | ema20 | vwap; berlaku untuk sisa posisi bila partial_r > 0
    trail_atr_mult: float = 1.5
    swing_bars: int = 5
    trail_buffer_atr: float = 0.3
    max_hold_bars: int = 96       # tutup paksa setelah N bar M15 (96 = 24 jam)


# ---------- waktu & sesi ----------
def server_to_utc(t: pd.Series) -> pd.Series:
    """Jam server broker -> UTC (naive). Server = New York + 7 jam dan ikut DST AS: di data, bar pertama tiap
    minggu selalu Senin 00:00 sepanjang tahun."""
    ny = (t - pd.Timedelta(hours=7)).dt.tz_localize(NY, ambiguous=np.ones(len(t), bool), nonexistent="shift_forward")
    return ny.dt.tz_convert("UTC").dt.tz_localize(None)


def add_clock(f: pd.DataFrame) -> None:
    """Kolom jam lokal London / New York (pecahan jam), hari trading (tanggal server) dan label sesi."""
    lon = server_to_utc(f["time"]).dt.tz_localize("UTC").dt.tz_convert(LONDON)
    ny = f["time"] - pd.Timedelta(hours=7)
    f["lon_h"] = lon.dt.hour + lon.dt.minute / 60
    f["ny_h"] = ny.dt.hour + ny.dt.minute / 60
    f["srv_h"] = f["time"].dt.hour + f["time"].dt.minute / 60
    f["day"] = f["time"].dt.normalize()
    in_lon, in_ny = (f["lon_h"] >= 8) & (f["lon_h"] < 17), (f["ny_h"] >= 8) & (f["ny_h"] < 17)
    f["session"] = np.select([in_lon & in_ny, in_ny, in_lon], ["Overlap", "NewYork", "London"], "Asia")


# ---------- struktur pasar ----------
def swing_state(high: np.ndarray, low: np.ndarray, close: np.ndarray, pivot: int = 3) -> dict[str, np.ndarray]:
    """Pivot fractal yang sudah terkonfirmasi (pivot di bar k baru terlihat di bar k + pivot) dan penembusannya.
    last_ph/last_pl = pivot high/low terakhir yang terlihat, struct = 1 (HH+HL) / -1 (LH+LL) / 0 (range),
    bos = 1/-1 di bar pertama yang close menembus pivot terakhir, choch = penembusan itu melawan struct."""
    n = len(high)
    hs, ls = pd.Series(high), pd.Series(low)
    w = 2 * pivot + 1
    is_ph = ((hs >= hs.rolling(w).max().shift(-pivot)) & (hs > hs.rolling(pivot).max().shift(1))).to_numpy()
    is_pl = ((ls <= ls.rolling(w).min().shift(-pivot)) & (ls < ls.rolling(pivot).min().shift(1))).to_numpy()
    last_ph, last_pl = np.full(n, np.nan), np.full(n, np.nan)
    struct, bos, choch = np.zeros(n, int), np.zeros(n, int), np.zeros(n, bool)
    ph = pl = prev_ph = prev_pl = np.nan
    ph_open = pl_open = False   # pivot terakhir belum ditembus
    for i in range(n):
        k = i - pivot
        if k >= 0:
            if is_ph[k]:
                prev_ph, ph, ph_open = ph, high[k], True
            if is_pl[k]:
                prev_pl, pl, pl_open = pl, low[k], True
        s = 0
        if ph > prev_ph and pl > prev_pl:
            s = 1
        elif ph < prev_ph and pl < prev_pl:
            s = -1
        last_ph[i], last_pl[i], struct[i] = ph, pl, s
        if ph_open and close[i] > ph:
            bos[i], choch[i], ph_open = 1, s == -1, False
        elif pl_open and close[i] < pl:
            bos[i], choch[i], pl_open = -1, s == 1, False
    return {"last_ph": last_ph, "last_pl": last_pl, "struct": struct, "bos": bos, "choch": choch}


def _bars_since(mask: np.ndarray) -> np.ndarray:
    idx = np.arange(len(mask))
    last = np.maximum.accumulate(np.where(mask, idx, -10 ** 9))
    return idx - last


def _range_level(f: pd.DataFrame, col: str, in_range: pd.Series, avail: pd.Series, how: str) -> pd.Series:
    """High/low berjalan dari bar in_range hari itu, hanya terlihat di bar `avail` (setelah range selesai)."""
    g = f[col].where(in_range).groupby(f["day"])
    run = g.cummax() if how == "max" else g.cummin()
    return run.groupby(f["day"]).ffill().where(avail)


# ---------- fitur ----------
def _htf(df: pd.DataFrame, tf: str, prefix: str) -> pd.DataFrame:
    t = add_indicators(df, BASE)
    return pd.DataFrame({
        "avail_time": t["time"] + TF_DELTA[tf],
        f"{prefix}_close": t["close"], f"{prefix}_ema20": t["ema_fast"], f"{prefix}_ema50": t["ema_slow"],
        f"{prefix}_ema200": t["ema_trend"], f"{prefix}_adx": t["adx"], f"{prefix}_atr": t["atr"],
        f"{prefix}_slope": (t["ema_slow"] - t["ema_slow"].shift(5)) / t["atr"],
        f"{prefix}_hi50": t["high"].rolling(50).max(), f"{prefix}_lo50": t["low"].rolling(50).min(),
    }).sort_values("avail_time")


def mtf_features(frames: dict[str, pd.DataFrame], ip: IntradayParams = IntradayParams()) -> pd.DataFrame:
    """Fitur per bar M15 + bias H1/H4 dari bar yang sudah close. frames butuh M15, H1, H4."""
    f = add_indicators(frames["M15"], BASE).reset_index(drop=True)
    f = f.rename(columns={"ema_fast": "ema20", "ema_slow": "ema50", "ema_trend": "ema200"})
    f["close_time"] = f["time"] + TF_DELTA["M15"]
    for tf, prefix in (("H1", "h1"), ("H4", "h4")):
        f = pd.merge_asof(f.sort_values("close_time"), _htf(frames[tf], tf, prefix), left_on="close_time",
                          right_on="avail_time", direction="backward").drop(columns="avail_time")
    f = f.reset_index(drop=True)
    add_clock(f)

    # Bias: h4_dir = sisi EMA200 H4; h4_strong = EMA50 H4 miring searah; h1_dir = EMA20 vs EMA50 H1 + kemiringan
    f["h4_dir"] = np.sign(f["h4_close"] - f["h4_ema200"]).fillna(0).astype(int)
    f["h4_strong"] = (np.sign(f["h4_slope"]).fillna(0) == f["h4_dir"]) & (f["h4_dir"] != 0)
    h1_up = (f["h1_ema20"] > f["h1_ema50"]) & (f["h1_slope"] > 0)
    h1_dn = (f["h1_ema20"] < f["h1_ema50"]) & (f["h1_slope"] < 0)
    f["h1_dir"] = np.where(h1_up, 1, np.where(h1_dn, -1, 0))

    # Regime dari H1: EMA sejajar + ADX
    al_up = (f["h1_ema20"] > f["h1_ema50"]) & (f["h1_ema50"] > f["h1_ema200"])
    al_dn = (f["h1_ema20"] < f["h1_ema50"]) & (f["h1_ema50"] < f["h1_ema200"])
    trend = (al_up | al_dn) & (f["h1_adx"] >= ip.adx_trend)
    f["regime"] = np.select([trend, f["h1_adx"] < ip.adx_range], ["TREND", "RANGE"], "MIXED")
    f["trend_dir"] = np.where(trend & al_up, 1, np.where(trend & al_dn, -1, 0))
    f["high_vol"] = f["atr"].rolling(ip.atr_pct_window, min_periods=100).rank(pct=True) >= ip.high_vol_pct

    # VWAP sesi (reset tiap hari trading = 17:00 New York) dan level harian
    vol = f["tick_volume"] if "tick_volume" in f else pd.Series(1.0, index=f.index)
    f["vwap"] = session_vwap(f["high"], f["low"], f["close"], vol, f["day"])
    f["bars_in_day"] = f.groupby("day").cumcount()
    daily = f.groupby("day").agg(hi=("high", "max"), lo=("low", "min")).shift(1)
    f["pdh"], f["pdl"] = f["day"].map(daily["hi"]), f["day"].map(daily["lo"])
    lon, ny, srv = f["lon_h"], f["ny_h"], f["srv_h"]
    ranges = {   # nama: (bar pembentuk range, jendela entry)
        "asia": ((srv < 12) & ((lon < 8) | (lon >= 20)), (lon >= 8) & (lon < 12)),
        "lon": ((lon >= 8) & (lon < 9), (lon >= 9) & (lon < 13)),
        "ny": ((ny >= 8) & (ny < 9), (ny >= 9) & (ny < 13)),
    }
    for name, (in_range, window) in ranges.items():
        f[f"{name}_hi"] = _range_level(f, "high", in_range, window, "max")
        f[f"{name}_lo"] = _range_level(f, "low", in_range, window, "min")

    # Struktur M15, pola candle, sweep, displacement
    hi, lo, cl, op = (f[c].to_numpy(float) for c in ("high", "low", "close", "open"))
    for k, v in swing_state(hi, lo, cl, ip.pivot).items():
        f[k] = v
    fl = candle_flags(f)
    rng = (f["high"] - f["low"]).replace(0, np.nan)
    f["bull_rej"] = fl["bull_engulf"] | fl["bull_pin"]
    f["bear_rej"] = fl["bear_engulf"] | fl["bear_pin"]
    f["bull_engulf"], f["bear_engulf"] = fl["bull_engulf"], fl["bear_engulf"]
    f["bull_close"] = ((f["close"] > f["open"]) & ((f["close"] - f["low"]) >= 0.7 * rng)).fillna(False)
    f["bear_close"] = ((f["close"] < f["open"]) & ((f["high"] - f["close"]) >= 0.7 * rng)).fillna(False)
    body = f["close"] - f["open"]
    f["disp"] = np.where(body >= ip.disp_atr * f["atr"], 1, np.where(-body >= ip.disp_atr * f["atr"], -1, 0))

    pc, pl_, ph_ = f["close"].shift(1), f["low"].shift(1), f["high"].shift(1)
    bull = pd.Series(False, index=f.index)
    bear = pd.Series(False, index=f.index)
    for lvl in ("last_pl", "pdl", "asia_lo"):   # wick menembus level lalu close kembali (1 atau 2 bar)
        L = f[lvl]
        bull |= (f["close"] > L) & ((f["low"] < L) | ((pl_ < L) & (pc <= L)))
    for lvl in ("last_ph", "pdh", "asia_hi"):
        L = f[lvl]
        bear |= (f["close"] < L) & ((f["high"] > L) | ((ph_ > L) & (pc >= L)))
    f["bull_sweep"], f["bear_sweep"] = bull, bear
    f["sweep_low"] = np.minimum(f["low"], pl_.fillna(f["low"]))
    f["sweep_high"] = np.maximum(f["high"], ph_.fillna(f["high"]))
    f["since_bull_sweep"] = _bars_since(bull.to_numpy())
    f["since_bear_sweep"] = _bars_since(bear.to_numpy())
    return f


# ---------- generator sinyal ----------
_COLS = ["i", "dir", "sl_ref", "strat"]
_WARM = ["ema200", "atr", "adx", "h1_ema200", "h1_adx", "h1_atr", "h4_ema200", "h4_slope"]


def _cands(f: pd.DataFrame, up: pd.Series, dn: pd.Series, sl_up: pd.Series, sl_dn: pd.Series,
           strat: str) -> pd.DataFrame:
    ok = f[_WARM].notna().all(axis=1)
    up, dn = (up & ok).to_numpy(), (dn & ok & ~up).to_numpy()
    i = np.flatnonzero(up | dn)
    d = np.where(up[i], 1, -1)
    return pd.DataFrame({"i": i, "dir": d, "sl_ref": np.where(d == 1, sl_up.to_numpy()[i], sl_dn.to_numpy()[i]),
                         "strat": strat}, columns=_COLS)


def signals_a(f: pd.DataFrame, ip: IntradayParams) -> pd.DataFrame:
    """A: tren H4/H1 (EMA200, EMA20>EMA50, EMA50 miring, ADX), pullback M15 ke EMA20/EMA50, candle rejection."""
    strong = f["h1_adx"] >= ip.adx_min_a
    ext = (f["close"] - f["ema20"]).abs() <= ip.max_ext_atr * f["atr"]
    up = ((f["h4_dir"] == 1) & (f["h1_dir"] == 1) & strong & (f["ema20"] > f["ema50"])
          & (f["low"].rolling(3).min() <= f["ema20"]) & (f["close"] > f["ema20"]) & f["bull_rej"] & ext)
    dn = ((f["h4_dir"] == -1) & (f["h1_dir"] == -1) & strong & (f["ema20"] < f["ema50"])
          & (f["high"].rolling(3).max() >= f["ema20"]) & (f["close"] < f["ema20"]) & f["bear_rej"] & ext)
    return _cands(f, up, dn, f["low"].rolling(5).min(), f["high"].rolling(5).max(), "A")


def signals_b(f: pd.DataFrame, ip: IntradayParams) -> pd.DataFrame:
    """B: sweep likuiditas searah bias H4 -> displacement + BOS/CHoCH M15 -> retrace ke FVG (atau order block) di
    zona discount/premium -> candle konfirmasi. SL di luar ujung sweep."""
    o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    disp, bos, h4 = f["disp"].to_numpy(), f["bos"].to_numpy(), f["h4_dir"].to_numpy()
    sweep = {1: f["bull_sweep"].to_numpy(), -1: f["bear_sweep"].to_numpy()}
    ext = {1: f["sweep_low"].to_numpy(float), -1: f["sweep_high"].to_numpy(float)}
    ok = f[_WARM].notna().all(axis=1).to_numpy()
    rows = []
    for d in (1, -1):
        stage, s_i, s_ext, seen_disp, zone, z_i = 0, 0, 0.0, False, (0.0, 0.0), 0
        for i in range(2, len(f)):
            if sweep[d][i] and h4[i] == d and ok[i] and stage < 2:
                # sweep baru (atau lebih dalam) memulai ulang penantian BOS
                if stage == 0 or d * (ext[d][i] - s_ext) < 0:
                    stage, s_i, s_ext, seen_disp = 1, i, ext[d][i], False
            if stage == 1:
                if i - s_i > ip.sweep_valid_bars or d * (c[i] - s_ext) < 0:
                    stage = 0
                    continue
                seen_disp |= disp[i] == d
                if bos[i] == d and seen_disp:
                    zone = _smc_zone(o, h, l, c, s_i, i, d)
                    leg_end = h[s_i:i + 1].max() if d == 1 else l[s_i:i + 1].min()
                    mid = (zone[0] + zone[1]) / 2 if zone else np.nan
                    # zona harus di separuh discount (buy) / premium (sell) dari leg sweep -> BOS
                    if zone and d * (mid - (s_ext + leg_end) / 2) <= 0:
                        stage, z_i = 2, i
                    else:
                        stage = 0
            elif stage == 2:
                if i - z_i > ip.zone_valid_bars or d * (c[i] - s_ext) < 0:
                    stage = 0
                    continue
                lo_z, hi_z = zone
                touched = l[i] <= hi_z if d == 1 else h[i] >= lo_z
                held = c[i] > lo_z if d == 1 else c[i] < hi_z
                if touched and held and d * (c[i] - o[i]) > 0:
                    rows.append((i, d, s_ext, "B"))
                    stage = 0
    return pd.DataFrame(rows, columns=_COLS).sort_values("i").reset_index(drop=True)


def _smc_zone(o, h, l, c, start: int, end: int, d: int) -> tuple[float, float] | None:
    """FVG 3-candle searah yang paling baru di antara sweep dan BOS; kalau tidak ada, order block = candle
    berlawanan terakhir sebelum BOS. Return (bawah, atas)."""
    for k in range(end, max(start, 2) - 1, -1):
        if d == 1 and l[k] > h[k - 2]:
            return h[k - 2], l[k]
        if d == -1 and h[k] < l[k - 2]:
            return h[k], l[k - 2]
    for k in range(end - 1, max(start - 3, 0) - 1, -1):
        if d * (c[k] - o[k]) < 0:
            return l[k], h[k]
    return None


def signals_c(f: pd.DataFrame, ip: IntradayParams) -> pd.DataFrame:
    """C: harga di sisi VWAP sesi, EMA20 vs EMA50 searah (M15 dan H1), pullback ke VWAP/EMA20, candle rejection."""
    mature = f["bars_in_day"] >= ip.vwap_min_bars
    tol = 0.1 * f["atr"]
    top, bot = np.maximum(f["vwap"], f["ema20"]), np.minimum(f["vwap"], f["ema20"])
    up = (mature & (f["h1_dir"] == 1) & (f["h4_dir"] != -1) & (f["ema20"] > f["ema50"]) & (f["close"] > top)
          & (f["low"].rolling(3).min() <= top + tol) & (f["bull_rej"] | f["bull_close"])
          & (f["close"] - top <= ip.max_ext_atr * f["atr"]))
    dn = (mature & (f["h1_dir"] == -1) & (f["h4_dir"] != 1) & (f["ema20"] < f["ema50"]) & (f["close"] < bot)
          & (f["high"].rolling(3).max() >= bot - tol) & (f["bear_rej"] | f["bear_close"])
          & (bot - f["close"] <= ip.max_ext_atr * f["atr"]))
    return _cands(f, up, dn, f["low"].rolling(5).min(), f["high"].rolling(5).max(), "C")


def signals_d(f: pd.DataFrame, ip: IntradayParams) -> pd.DataFrame:
    """D: breakout range Asia / pembukaan London / pembukaan New York dengan momentum, lalu retest level yang
    ditembus dan candle konfirmasi. Satu sinyal per range per hari."""
    o, h, l, c, a = (f[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    e20, e50, day = f["ema20"].to_numpy(float), f["ema50"].to_numpy(float), f["day"].to_numpy()
    ok = f[_WARM].notna().all(axis=1).to_numpy()
    rows = []
    for name in ("asia", "lon", "ny"):
        hi, lo = f[f"{name}_hi"].to_numpy(float), f[f"{name}_lo"].to_numpy(float)
        cur, state, b_i, d = None, 0, 0, 0   # state: 0 menunggu breakout, 1 menunggu retest, 2 selesai hari ini
        for i in range(1, len(f)):
            if day[i] != cur:
                cur, state = day[i], 0
            if state == 2 or np.isnan(hi[i]) or not ok[i]:
                continue
            if state == 0:
                up = c[i] > hi[i] and c[i - 1] <= hi[i] and e20[i] > e50[i]
                dn = c[i] < lo[i] and c[i - 1] >= lo[i] and e20[i] < e50[i]
                if up or dn:
                    d = 1 if up else -1
                    lvl = hi[i] if up else lo[i]
                    # momentum: body searah >= 0.5 ATR atau close sudah 0.2 ATR di luar range
                    if d * (c[i] - o[i]) >= 0.5 * a[i] or d * (c[i] - lvl) >= 0.2 * a[i]:
                        state, b_i = 1, i
                    else:
                        state = 2
                continue
            lvl = hi[i] if d == 1 else lo[i]
            if i - b_i > ip.retest_bars or d * (c[i] - lvl) < -0.5 * a[i]:
                state = 2   # tidak ada retest, atau breakout gagal (close jauh kembali ke dalam range)
                continue
            touched = l[i] <= lvl + 0.25 * a[i] if d == 1 else h[i] >= lvl - 0.25 * a[i]
            if touched and d * (c[i] - lvl) > 0 and d * (c[i] - o[i]) > 0:
                if d * (c[i] - lvl) <= ip.max_ext_atr * a[i]:
                    sl_ref = l[b_i:i + 1].min() if d == 1 else h[b_i:i + 1].max()
                    rows.append((i, d, sl_ref, "D"))
                state = 2
    return pd.DataFrame(rows, columns=_COLS).sort_values("i").reset_index(drop=True)


def signals_e(f: pd.DataFrame, ip: IntradayParams) -> pd.DataFrame:
    """E: hanya di regime RANGE. Sentuh Bollinger Band + RSI ekstrem, lalu close kembali ke dalam band dengan
    candle pembalikan."""
    rng = f["regime"] == "RANGE"
    up = (rng & (f["low"].rolling(2).min() <= f["bb_low"]) & (f["rsi"].rolling(2).min() < ip.rsi_low)
          & (f["close"] > f["bb_low"]) & (f["bull_rej"] | f["bull_close"]))
    dn = (rng & (f["high"].rolling(2).max() >= f["bb_up"]) & (f["rsi"].rolling(2).max() > ip.rsi_high)
          & (f["close"] < f["bb_up"]) & (f["bear_rej"] | f["bear_close"]))
    return _cands(f, up, dn, f["low"].rolling(3).min(), f["high"].rolling(3).max(), "E")


GENERATORS = {"A": signals_a, "B": signals_b, "C": signals_c, "D": signals_d, "E": signals_e}
REGIME_STRATEGIES = {"TREND": ("A", "B", "C", "D"), "RANGE": ("E", "B"), "MIXED": ()}


def all_signals(f: pd.DataFrame, ip: IntradayParams = IntradayParams()) -> pd.DataFrame:
    """Kandidat semua strategi (urut bar), dengan regime bar sinyal."""
    parts = [g(f, ip) for g in GENERATORS.values()]
    out = pd.concat([p for p in parts if len(p)] or parts[:1], ignore_index=True)
    out = out.astype({"i": int, "dir": int, "sl_ref": float})
    out["regime"] = f["regime"].to_numpy()[out["i"].to_numpy()]
    return out.sort_values(["i", "strat"]).reset_index(drop=True)


def hybrid(cands: pd.DataFrame) -> pd.DataFrame:
    """F: hanya strategi yang cocok dengan regime bar sinyal (MIXED = tidak trade)."""
    keep = np.array([s in REGIME_STRATEGIES[r] for s, r in zip(cands["strat"], cands["regime"])], dtype=bool)
    return cands[keep].reset_index(drop=True)   # array boolean: tanpa kandidat pun kolom tetap ada


# ---------- skor confluence ----------
def score_parts(a: dict[str, np.ndarray], i: int, d: int, room_r: float) -> dict[str, int]:
    """Komponen skor di bar sinyal i untuk arah d (bobot SCORE_WEIGHTS). room_r = jarak ke support/resistance
    besar di depan entry dalam R. Skor adalah filter, bukan probabilitas menang."""
    up = d == 1
    atr_ = a["atr"][i]
    adx_ = a["h1_adx"][i]
    since = a["since_bull_sweep" if up else "since_bear_sweep"][i]
    engulf, rej, strong = (a[f"{'bull' if up else 'bear'}_{k}"][i] for k in ("engulf", "rej", "close"))
    dist = min(abs(a["close"][i] - a["ema20"][i]), abs(a["close"][i] - a["vwap"][i])) / atr_ if atr_ > 0 else 9.0
    return {
        "trend": 8 * (a["h4_dir"][i] == d) + 4 * bool(a["h4_strong"][i] and a["h4_dir"][i] == d)
        + 8 * (a["h1_dir"][i] == d),
        "ema": 5 * (d * (a["ema20"][i] - a["ema50"][i]) > 0) + 5 * (d * (a["close"][i] - a["ema200"][i]) > 0),
        "momentum": 10 if adx_ >= 25 else 6 if adx_ >= 20 else 3 if adx_ >= 15 else 0,
        "structure": 15 if a["struct"][i] == d else 7 if a["struct"][i] == 0 else 0,
        "liquidity": 15 if since <= 12 else 8 if since <= 32 else 0,
        "price_action": 15 if engulf else 12 if rej else 6 if strong else 0,
        "location": 10 if dist <= 0.5 else 6 if dist <= 1.0 else 3 if dist <= 1.5 else 0,
        "rr": 5 if room_r >= 2 else 3 if room_r >= 1.5 else 0,
    }


def opposing_level(a: dict[str, np.ndarray], i: int, d: int, price: float, strat: str = "") -> float:
    """Support/resistance besar terdekat di depan harga: high/low kemarin dan swing 50 bar H1
    (strategi E: garis tengah Bollinger). NaN bila tidak ada."""
    if strat == "E":
        mid = a["bb_mid"][i]
        return float(mid) if d * (mid - price) > 0 else np.nan
    levels = [a[k][i] for k in (("pdh", "h1_hi50") if d == 1 else ("pdl", "h1_lo50"))]
    ahead = [x for x in levels if not np.isnan(x) and d * (x - price) > 0]
    return float(min(ahead) if d == 1 else max(ahead)) if ahead else np.nan
