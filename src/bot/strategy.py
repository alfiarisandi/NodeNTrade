"""Strategi trend following. Fungsi di sini murni (tanpa MT5) dan dipakai oleh backtest maupun live.

Format DataFrame bar: kolom time (open time bar, jam server broker), open, high, low, close, spread.
Harga bar MT5 adalah harga BID.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.config import SessionCfg, StrategyCfg
from bot.indicators import adx, atr, ema, linreg_end, rsi, supertrend

TF_DELTA = {"M1": pd.Timedelta(minutes=1), "M5": pd.Timedelta(minutes=5), "M15": pd.Timedelta(minutes=15),
            "M30": pd.Timedelta(minutes=30), "H1": pd.Timedelta(hours=1), "H4": pd.Timedelta(hours=4),
            "D1": pd.Timedelta(days=1)}


@dataclass
class Signal:
    symbol: str
    direction: int            # 1 = buy, -1 = sell
    bar_time: pd.Timestamp    # open time bar sinyal (jam server)
    close: float
    atr: float
    sl_distance: float        # jarak harga entry -> SL
    tp_distance: float        # 0 = tanpa TP (keluar lewat trailing stop / SL)
    snapshot: dict = field(default_factory=dict)

    @property
    def side(self) -> str:
        return "BUY" if self.direction == 1 else "SELL"


def add_indicators(df: pd.DataFrame, p: StrategyCfg) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], p.ema_fast)
    out["ema_slow"] = ema(out["close"], p.ema_slow)
    out["ema_trend"] = ema(out["close"], p.ema_trend)
    out["atr"] = atr(out["high"], out["low"], out["close"], p.atr_period)
    out["adx"] = adx(out["high"], out["low"], out["close"], p.adx_period)
    out["rsi"] = rsi(out["close"], p.rsi_period)
    # Channel dari N bar SEBELUM bar ini (bar ini sendiri tidak ikut, supaya breakout terdeteksi)
    out["dc_high"] = out["high"].rolling(p.donchian_period).max().shift(1)
    out["dc_low"] = out["low"].rolling(p.donchian_period).min().shift(1)
    out["bb_mid"] = out["close"].rolling(p.bb_period).mean()
    bb_sd = out["close"].rolling(p.bb_period).std(ddof=0)
    out["bb_up"] = out["bb_mid"] + p.bb_std * bb_sd
    out["bb_low"] = out["bb_mid"] - p.bb_std * bb_sd
    out["rsi_fast"] = rsi(out["close"], p.rsi_fast_period)
    macd = ema(out["close"], 12) - ema(out["close"], 26)
    out["macd"], out["macd_sig"] = macd, ema(macd, 9)
    out["atr_ratio"] = out["atr"] / out["atr"].rolling(50).mean()
    _add_entry_indicators(out, p)
    # Buang periode warm-up supaya nilai indikator awal yang belum stabil tidak dipakai
    out.loc[out.index[: p.ema_slow], ["ema_fast", "ema_slow"]] = np.nan
    out.loc[out.index[: p.ema_trend], "ema_trend"] = np.nan
    out.loc[out.index[: 3 * p.adx_period], ["adx", "atr", "rsi"]] = np.nan
    return out


def _add_entry_indicators(out: pd.DataFrame, p: StrategyCfg) -> None:
    """Indikator khusus strategi open source, hanya dihitung jika strategi itu dipakai."""
    h, l, c = out["high"], out["low"], out["close"]
    if p.entry_type == "supertrend":
        out["st_dir"] = supertrend(h, l, c, p.st_period, p.st_mult)
    elif p.entry_type == "squeeze":
        # LazyBear Squeeze Momentum: BB(20,2) di dalam Keltner(20,1.5) = squeeze
        n = p.bb_period
        mid = c.rolling(n).mean()
        sd = c.rolling(n).std(ddof=0)
        rng = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        kc = rng.rolling(n).mean() * p.kc_mult
        out["sqz_on"] = ((mid - p.bb_std * sd) > (mid - kc)) & ((mid + p.bb_std * sd) < (mid + kc))
        center = ((h.rolling(n).max() + l.rolling(n).min()) / 2 + mid) / 2
        out["sqz_mom"] = linreg_end(c - center, n)
    elif p.entry_type == "tsmom":
        out["tsmom_ret"] = c / c.shift(p.tsmom_lookback) - 1
    elif p.entry_type == "ichimoku":
        tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
        kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
        span_a = ((tenkan + kijun) / 2).shift(26)
        span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
        out["tenkan"], out["kijun"] = tenkan, kijun
        out["cloud_top"] = pd.concat([span_a, span_b], axis=1).max(axis=1, skipna=False)
        out["cloud_bot"] = pd.concat([span_a, span_b], axis=1).min(axis=1, skipna=False)


def candle_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Pola candle per bar (bool): bull_engulf, bear_engulf, bull_pin, bear_pin, inside.
    Pin bar = ekor >= 2x body dan >= 60% range bar. Engulfing = body menutup body bar sebelumnya yang berlawanan."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper, lower = h - np.maximum(o, c), np.minimum(o, c) - l
    po, pc = o.shift(), c.shift()
    prev_body = (pc - po).abs()
    return pd.DataFrame({
        "bull_engulf": (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > prev_body),
        "bear_engulf": (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > prev_body),
        "bull_pin": (lower >= 2 * body) & (lower >= 0.6 * rng),
        "bear_pin": (upper >= 2 * body) & (upper >= 0.6 * rng),
        "inside": (h < h.shift()) & (l > l.shift()),
    }, index=df.index).fillna(False).astype(bool)


def compute_features(entry: pd.DataFrame, trend: pd.DataFrame, p: StrategyCfg,
                     offset_hours: int = 0) -> pd.DataFrame:
    """Indikator timeframe entry + filter tren dari bar timeframe tren yang SUDAH close (tanpa look-ahead).
    Kolom tren diberi prefix h4_ apa pun timeframe trennya. offset_hours = selisih jam server - UTC
    (dipakai strategi day trade yang memakai jam/tanggal UTC)."""
    f = add_indicators(entry, p).reset_index(drop=True)
    f["close_time"] = f["time"] + TF_DELTA[p.entry_tf]
    if p.entry_type in DAY_LEVEL_TYPES:
        _add_day_levels(f, p, offset_hours)

    t = add_indicators(trend, p)[["time", "close", "ema_trend", "adx"]].copy()
    t["avail_time"] = t["time"] + TF_DELTA[p.trend_tf]
    t = t.rename(columns={"close": "h4_close", "ema_trend": "h4_ema_trend", "adx": "h4_adx"}).drop(columns="time")

    f = pd.merge_asof(f.sort_values("close_time"), t.sort_values("avail_time"),
                      left_on="close_time", right_on="avail_time", direction="backward")
    f["trend"] = np.sign(f["h4_close"] - f["h4_ema_trend"])
    return f


DAY_LEVEL_TYPES = ("range_breakout", "pdh_breakout")
MIN_LEVEL_SL_ATR = 0.5   # SL level tidak lebih sempit dari 0.5 ATR (range sangat sempit = biaya terlalu besar)


def _add_day_levels(f: pd.DataFrame, p: StrategyCfg, offset_hours: int) -> None:
    """Level harian (UTC) + kolom brk: arah breakout PERTAMA hari itu di jendela entry (1/-1/0).
    Level hanya diisi di jendela entry, saat semua bar pembentuk level sudah close (tanpa look-ahead)."""
    utc = f["time"] - pd.Timedelta(hours=offset_hours)
    day, hour = utc.dt.floor("D"), utc.dt.hour
    if p.entry_type == "range_breakout":
        in_rng = (hour >= p.range_start_utc) & (hour < p.range_end_utc)
        hi = f["high"].where(in_rng).groupby(day).transform("max")
        lo = f["low"].where(in_rng).groupby(day).transform("min")
    else:  # pdh_breakout: high/low hari UTC sebelumnya
        daily = f.groupby(day).agg(hi=("high", "max"), lo=("low", "min")).shift(1)
        hi, lo = day.map(daily["hi"]), day.map(daily["lo"])
    window = (hour >= p.range_end_utc) & (hour < p.entry_window_end_utc)
    f["lvl_hi"], f["lvl_lo"] = hi.where(window), lo.where(window)
    up = (f["close"] > f["lvl_hi"]).to_numpy()
    dn = (f["close"] < f["lvl_lo"]).to_numpy()
    raw = pd.Series(np.where(up, 1, np.where(dn, -1, 0)))
    first = (raw != 0).groupby(day.to_numpy()).cumsum().to_numpy() == 1
    f["brk"] = np.where(first & (raw != 0), raw, 0)


def in_session(close_time_server: pd.Timestamp, offset_hours: int, s: SessionCfg, hours: bool = True) -> bool:
    utc = close_time_server - pd.Timedelta(hours=offset_hours)
    if utc.weekday() >= 5:
        return False
    if utc.weekday() == 4 and utc.hour >= s.friday_cutoff_hour_utc:
        return False
    return not hours or s.start_hour_utc <= utc.hour < s.end_hour_utc


_NEEDED = ["ema_fast", "ema_slow", "atr", "adx", "rsi", "trend"]
MEAN_REVERSION = ("bb_reversion", "rsi_reversion")
TREND_RSI_FILTERED = ("ema_cross", "pullback", "donchian")  # strategi lain punya aturan sendiri


def _raw_direction(f: pd.DataFrame, p: StrategyCfg) -> np.ndarray:
    """Arah sinyal mentah per bar (1/-1/0) sesuai entry_type, sebelum filter tren/ADX/RSI/sesi.
    Perbandingan dengan NaN selalu False, jadi bar warm-up otomatis 0."""
    prev = f.shift(1)
    if p.entry_type == "ema_cross":
        up = (prev["ema_fast"] <= prev["ema_slow"]) & (f["ema_fast"] > f["ema_slow"])
        dn = (prev["ema_fast"] >= prev["ema_slow"]) & (f["ema_fast"] < f["ema_slow"])
    elif p.entry_type == "pullback":
        # Struktur tren searah, harga sempat di bawah EMA fast lalu close kembali di atasnya
        up = (f["ema_fast"] > f["ema_slow"]) & (prev["close"] <= prev["ema_fast"]) & (f["close"] > f["ema_fast"])
        dn = (f["ema_fast"] < f["ema_slow"]) & (prev["close"] >= prev["ema_fast"]) & (f["close"] < f["ema_fast"])
    elif p.entry_type == "donchian":
        up = (f["close"] > f["dc_high"]) & (prev["close"] <= prev["dc_high"])
        dn = (f["close"] < f["dc_low"]) & (prev["close"] >= prev["dc_low"])
    elif p.entry_type == "bb_reversion":
        # Sempat close di luar band, lalu close kembali ke dalam band
        up = (prev["close"] < prev["bb_low"]) & (f["close"] > f["bb_low"])
        dn = (prev["close"] > prev["bb_up"]) & (f["close"] < f["bb_up"])
    elif p.entry_type == "rsi_reversion":
        up = f["rsi_fast"] < p.rsi_fast_low
        dn = f["rsi_fast"] > p.rsi_fast_high
    elif p.entry_type == "supertrend":
        up = (f["st_dir"] == 1) & (prev["st_dir"] == -1)
        dn = (f["st_dir"] == -1) & (prev["st_dir"] == 1)
    elif p.entry_type == "squeeze":
        # Squeeze baru lepas, momentum searah dan menguat
        fired = prev["sqz_on"].fillna(False).astype(bool) & ~f["sqz_on"].astype(bool)
        up = fired & (f["sqz_mom"] > 0) & (f["sqz_mom"] > prev["sqz_mom"])
        dn = fired & (f["sqz_mom"] < 0) & (f["sqz_mom"] < prev["sqz_mom"])
    elif p.entry_type == "tsmom":
        up = (f["tsmom_ret"] > 0) & (prev["tsmom_ret"] <= 0)
        dn = (f["tsmom_ret"] < 0) & (prev["tsmom_ret"] >= 0)
    elif p.entry_type in DAY_LEVEL_TYPES:
        return f["brk"].fillna(0).to_numpy(int)
    elif p.entry_type == "ichimoku":
        up = (prev["tenkan"] <= prev["kijun"]) & (f["tenkan"] > f["kijun"]) & (f["close"] > f["cloud_top"])
        dn = (prev["tenkan"] >= prev["kijun"]) & (f["tenkan"] < f["kijun"]) & (f["close"] < f["cloud_bot"])
    else:  # macd_ema200
        cross_up = (prev["macd"] <= prev["macd_sig"]) & (f["macd"] > f["macd_sig"])
        cross_dn = (prev["macd"] >= prev["macd_sig"]) & (f["macd"] < f["macd_sig"])
        up = cross_up & (f["macd"] < 0) & (f["close"] > f["ema_trend"])
        dn = cross_dn & (f["macd"] > 0) & (f["close"] < f["ema_trend"])
    return np.where(up, 1, np.where(dn, -1, 0))


def exit_flags(f: pd.DataFrame, p: StrategyCfg) -> tuple[np.ndarray, np.ndarray] | None:
    """(keluar_long, keluar_short) per bar saat close, untuk exit_rule = "signal"."""
    if p.exit_rule != "signal":
        return None
    prev = f.shift(1)
    if p.entry_type == "supertrend":
        xl, xs = f["st_dir"] == -1, f["st_dir"] == 1
    elif p.entry_type == "squeeze":
        xl, xs = f["sqz_mom"] < prev["sqz_mom"], f["sqz_mom"] > prev["sqz_mom"]  # momentum berganti warna
    elif p.entry_type == "tsmom":
        xl, xs = f["tsmom_ret"] < 0, f["tsmom_ret"] > 0
    elif p.entry_type == "ichimoku":
        xl, xs = f["tenkan"] < f["kijun"], f["tenkan"] > f["kijun"]
    else:
        raw = _raw_direction(f, p)
        xl, xs = raw == -1, raw == 1
    return np.asarray(xl, bool), np.asarray(xs, bool)


def session_mask(close_time: pd.Series, offset_hours: int, s: SessionCfg, hours: bool = True) -> np.ndarray:
    utc = close_time - pd.Timedelta(hours=offset_hours)
    wd, hr = utc.dt.day_of_week, utc.dt.hour
    ok = (wd < 5) & ~((wd == 4) & (hr >= s.friday_cutoff_hour_utc))
    if hours:
        ok &= (hr >= s.start_hour_utc) & (hr < s.end_hour_utc)
    return ok.to_numpy()


def in_session(close_time_server: pd.Timestamp, offset_hours: int, s: SessionCfg, hours: bool = True) -> bool:
    return bool(session_mask(pd.Series([close_time_server]), offset_hours, s, hours)[0])


def signal_directions(f: pd.DataFrame, p: StrategyCfg, s: SessionCfg, offset_hours: int) -> np.ndarray:
    """Arah sinyal final per bar (1/-1/0), semua filter sudah diterapkan."""
    d = _raw_direction(f, p)
    valid = f[_NEEDED].notna().all(axis=1) & f["ema_fast"].shift(1).notna() & f["ema_slow"].shift(1).notna()
    ok = valid.to_numpy() & (d != 0)
    if p.trend_filter == "with":
        ok &= f["trend"].to_numpy() == d
    adx_ = f["adx"].to_numpy()
    ok &= (adx_ >= p.adx_min) & (adx_ < p.adx_max)
    if p.entry_type in TREND_RSI_FILTERED:
        rsi_ = f["rsi"].to_numpy()
        ok &= ~(((d == 1) & (rsi_ >= p.rsi_buy_max)) | ((d == -1) & (rsi_ <= p.rsi_sell_min)))
    if p.tp_mode == "mid_band":
        ok &= d * (f["bb_mid"].to_numpy() - f["close"].to_numpy()) > 0
    if p.ma_filter:
        ok &= d * (f["close"].to_numpy() - f["ema_trend"].to_numpy()) > 0
    if p.macd_filter:
        ok &= d * (f["macd"].to_numpy() - f["macd_sig"].to_numpy()) > 0
    if p.atr_ratio_min > 0:
        ok &= f["atr_ratio"].to_numpy() >= p.atr_ratio_min
    if p.ma50_filter:
        ok &= d * (f["close"].to_numpy() - f["ema_slow"].to_numpy()) > 0
    if p.candle_confirm != "none":
        fl = candle_flags(f)
        kinds = {"engulfing": ["engulf"], "pin": ["pin"], "any": ["engulf", "pin"]}[p.candle_confirm]
        bull = np.logical_or.reduce([fl[f"bull_{k}"].to_numpy() for k in kinds])
        bear = np.logical_or.reduce([fl[f"bear_{k}"].to_numpy() for k in kinds])
        ok &= np.where(d == 1, bull, np.where(d == -1, bear, False))
    ok &= session_mask(f["close_time"], offset_hours, s, p.session_hours)
    return np.where(ok, d, 0)


SNAPSHOT_COLS = ["ema_fast", "ema_slow", "adx", "rsi", "atr", "h4_close", "h4_ema_trend", "h4_adx",
                 "tenkan", "kijun", "cloud_top", "cloud_bot", "st_dir", "sqz_mom", "tsmom_ret", "macd", "macd_sig",
                 "atr_ratio", "lvl_hi", "lvl_lo"]


def make_signal(f: pd.DataFrame, i: int, d: int, p: StrategyCfg, symbol: str = "") -> Signal:
    row = f.iloc[i]
    a = float(row["atr"])
    sl_distance = p.sl_atr_mult * a
    if p.sl_mode == "level" and "lvl_hi" in row.index:
        level = row["lvl_lo"] if d == 1 else row["lvl_hi"]
        if pd.notna(level):
            sl_distance = min(max(abs(float(row["close"]) - float(level)), MIN_LEVEL_SL_ATR * a), sl_distance)
    if p.tp_r > 0:
        tp_distance = p.tp_r * sl_distance
    elif p.tp_mode == "mid_band":
        tp_distance = d * (float(row["bb_mid"]) - float(row["close"]))
    else:
        tp_distance = max(p.tp_atr_mult, 0.0) * a
    return Signal(
        symbol=symbol,
        direction=d,
        bar_time=row["time"],
        close=float(row["close"]),
        atr=a,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
        snapshot={k: round(float(row[k]), 6) for k in SNAPSHOT_COLS if k in row.index and pd.notna(row[k])},
    )


def signal_at(f: pd.DataFrame, i: int, p: StrategyCfg, s: SessionCfg, offset_hours: int,
              symbol: str = "") -> Signal | None:
    """Evaluasi bar ke-i (bar yang sudah close). Entry dilakukan di open bar berikutnya."""
    if i < 1:
        return None
    d = int(signal_directions(f.iloc[i - 1:i + 1], p, s, offset_hours)[-1])
    return make_signal(f, i, d, p, symbol) if d else None


def simulate_exit(open_: np.ndarray, high: np.ndarray, low: np.ndarray, spread: np.ndarray,
                  start: int, direction: int, entry: float, sl: float, tp: float | None,
                  be_trigger: float = 0.0, trail_dist: float = 0.0, close: np.ndarray | None = None,
                  max_bars: int = 0, exit_flag: np.ndarray | None = None,
                  lock_trigger: float = 0.0, lock_fraction: float = 0.0,
                  swing_bars: int = 0, trail_level: np.ndarray | None = None,
                  trail_buffer: float = 0.0) -> tuple[int, float, str] | None:
    """Jalankan posisi bar demi bar mulai index `start`. Harga bar = BID.
    swing_bars > 0: SL mengikuti low terendah (buy) / high tertinggi (sell) dari N bar terakhir, hanya mengetat.
    trail_level: level per bar (mis. EMA20 / VWAP); SL mengikuti level -/+ trail_buffer, hanya mengetat. Level yang
    sudah terlewati harga = keluar di open bar berikutnya.

    Buy keluar di BID, sell keluar di ASK (= bid + spread). Konservatif:
    - jika SL dan TP tersentuh di bar yang sama, dianggap kena SL
    - gap melewati SL/TP dieksekusi di harga open
    - breakeven, trailing stop & kunci profit baru berlaku mulai bar SETELAH bar pemicunya
    tp=None berarti tanpa TP. max_bars > 0 = tutup di close bar ke-N (butuh `close`).
    lock_trigger > 0: setelah profit maksimum >= lock_trigger, SL ikut di lock_fraction x profit maksimum.
    exit_flag[j] True = tutup di close bar j (aturan exit strategi, butuh `close`). Return (index_bar_keluar, harga_keluar, alasan) atau None jika belum keluar.
    """
    cur_sl, moved, mfe = sl, "", 0.0   # mfe = profit maksimum (jarak harga) yang pernah dicapai
    for j in range(start, len(high)):
        if direction == 1:
            o, h, l = open_[j], high[j], low[j]
            if o <= cur_sl:
                return j, o, moved or "sl"
            if l <= cur_sl:
                return j, cur_sl, moved or "sl"
            if tp is not None and o >= tp:
                return j, o, "tp"
            if tp is not None and h >= tp:
                return j, tp, "tp"
            if be_trigger and cur_sl < entry and h - entry >= be_trigger:
                cur_sl, moved = entry, "be"
            if trail_dist and h - trail_dist > cur_sl:
                cur_sl = h - trail_dist
                moved = "trail" if cur_sl > entry else moved
            mfe = max(mfe, h - entry)
            if lock_trigger and mfe >= lock_trigger and entry + lock_fraction * mfe > cur_sl:
                cur_sl, moved = entry + lock_fraction * mfe, "lock"
            if swing_bars and j - start + 1 >= swing_bars and low[j - swing_bars + 1:j + 1].min() > cur_sl:
                cur_sl, moved = low[j - swing_bars + 1:j + 1].min(), "trail"
            if trail_level is not None and trail_level[j] - trail_buffer > cur_sl:
                cur_sl = trail_level[j] - trail_buffer
                moved = "trail" if cur_sl > entry else moved
        else:
            sp = spread[j]
            o, h, l = open_[j] + sp, high[j] + sp, low[j] + sp
            if o >= cur_sl:
                return j, o, moved or "sl"
            if h >= cur_sl:
                return j, cur_sl, moved or "sl"
            if tp is not None and o <= tp:
                return j, o, "tp"
            if tp is not None and l <= tp:
                return j, tp, "tp"
            if be_trigger and cur_sl > entry and entry - l >= be_trigger:
                cur_sl, moved = entry, "be"
            if trail_dist and l + trail_dist < cur_sl:
                cur_sl = l + trail_dist
                moved = "trail" if cur_sl < entry else moved
            mfe = max(mfe, entry - l)
            if lock_trigger and mfe >= lock_trigger and entry - lock_fraction * mfe < cur_sl:
                cur_sl, moved = entry - lock_fraction * mfe, "lock"
            if swing_bars and j - start + 1 >= swing_bars:
                sw = (high[j - swing_bars + 1:j + 1] + spread[j - swing_bars + 1:j + 1]).max()
                if sw < cur_sl:
                    cur_sl, moved = sw, "trail"
            if trail_level is not None and trail_level[j] + sp + trail_buffer < cur_sl:
                cur_sl = trail_level[j] + sp + trail_buffer
                moved = "trail" if cur_sl < entry else moved
        if close is not None and exit_flag is not None and exit_flag[j]:
            return j, close[j] + (spread[j] if direction == -1 else 0.0), "signal"
        if max_bars and close is not None and j - start + 1 >= max_bars:
            return j, close[j] + (spread[j] if direction == -1 else 0.0), "time"
    return None


def simulate_managed(open_: np.ndarray, high: np.ndarray, low: np.ndarray, spread: np.ndarray, close: np.ndarray,
                     start: int, direction: int, entry: float, sl: float, partial_at: float, partial_frac: float,
                     tp2: float | None = None, be_buffer: float = 0.0, post_trail: float = 0.0,
                     post_swing_bars: int = 0, max_bars: int = 0, post_trail_level: np.ndarray | None = None,
                     post_trail_buffer: float = 0.0, keep_sl: bool = False,
                     **first) -> tuple[int, float, str] | None:
    """Partial TP + dynamic SL: tutup partial_frac di entry +/- partial_at (TP1); sisa posisi lanjut dengan SL di
    entry +/- be_buffer (breakeven + biaya), TP di tp2 (None = tanpa), trailing post_trail / post_swing_bars /
    post_trail_level (level per bar, lihat simulate_exit). keep_sl = sisa posisi tetap memakai SL awal (tanpa
    breakeven). Aturan exit strategi (exit_flag di `first`) tetap berlaku untuk sisa posisi.
    Harga keluar yang dikembalikan = rata-rata tertimbang kedua bagian, jadi R trade tetap d*(harga-entry)/SL.
    `first` = argumen simulate_exit untuk fase sebelum TP1 (be_trigger, lock_trigger, ...)."""
    d = direction
    a = simulate_exit(open_, high, low, spread, start, d, entry, sl, entry + d * partial_at, close=close,
                      max_bars=max_bars, **first)
    if a is None or a[2] != "tp":
        return a
    j1, p1, _ = a
    left = max_bars - (j1 - start + 1) if max_bars else 0
    if j1 + 1 >= len(high) or (max_bars and left <= 0):
        j2, p2, why = j1, close[j1] + (spread[j1] if d == -1 else 0.0), "time"
    else:
        b = simulate_exit(open_, high, low, spread, j1 + 1, d, entry, sl if keep_sl else entry + d * be_buffer, tp2,
                          trail_dist=post_trail, close=close, max_bars=left, swing_bars=post_swing_bars,
                          trail_level=post_trail_level, trail_buffer=post_trail_buffer,
                          exit_flag=first.get("exit_flag"))
        j2, p2, why = b if b is not None else (len(high) - 1, close[-1] + (spread[-1] if d == -1 else 0.0), "eod")
        why = "be" if why == "sl" and not keep_sl else why
    return j2, partial_frac * p1 + (1 - partial_frac) * p2, f"partial+{why}"
