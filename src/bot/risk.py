"""Manajemen risiko: ukuran lot, batas rugi harian, eksposur mata uang."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime


def calc_lot(risk_amount: float, sl_distance: float, tick_size: float, tick_value: float,
             volume_step: float, volume_min: float, volume_max: float) -> float:
    """Lot sehingga kerugian di SL <= risk_amount. Dibulatkan ke BAWAH.

    Return 0 jika lot minimum broker sudah melebihi risiko (trade dilewati, bukan dipaksa).
    """
    if risk_amount <= 0 or sl_distance <= 0 or tick_size <= 0 or tick_value <= 0 or volume_step <= 0:
        return 0.0
    loss_per_lot = sl_distance / tick_size * tick_value
    lot = math.floor(risk_amount / loss_per_lot / volume_step + 1e-9) * volume_step
    lot = min(lot, volume_max)
    if lot < volume_min - 1e-12:
        return 0.0
    decimals = max(0, -int(math.floor(math.log10(volume_step)))) if volume_step < 1 else 0
    return round(lot, decimals)


def lot_with_min(equity: float, risk: float, max_risk: float, sl_distance: float, meta: dict) -> float:
    """Lot untuk risiko `risk` x equity. Jika itu di bawah lot minimum broker (akun kecil), pakai lot
    minimum selama kerugiannya di SL <= max_risk x equity; selain itu 0 (sinyal dilewati)."""
    lot = calc_lot(equity * risk, sl_distance, meta["tick_size"], meta["tick_value"],
                   meta["volume_step"], meta["volume_min"], meta["volume_max"])
    if lot == 0 and meta["tick_size"] > 0 and sl_distance > 0:
        loss_min_lot = sl_distance / meta["tick_size"] * meta["tick_value"] * meta["volume_min"]
        if loss_min_lot <= equity * max_risk:
            lot = meta["volume_min"]
    return lot


def split_currencies(symbol: str) -> tuple[str, str]:
    """EURUSD.m -> ("EUR", "USD"), XAUUSD -> ("XAU", "USD")."""
    s = symbol.upper()
    return s[:3], s[3:6]


def currency_exposure(positions: list[tuple[str, int]]) -> dict[str, int]:
    """positions: [(symbol, direction)]. Buy EURUSD = +EUR, -USD."""
    exp: dict[str, int] = {}
    for sym, d in positions:
        base, quote = split_currencies(sym)
        exp[base] = exp.get(base, 0) + d
        exp[quote] = exp.get(quote, 0) - d
    return exp


def check_new_trade(symbol: str, direction: int, open_positions: list[tuple[str, int]],
                    max_open: int, max_exposure: int,
                    same_source: list[tuple[str, int]] | None = None) -> str | None:
    """Return alasan penolakan, atau None jika trade boleh dibuka. Batas jumlah posisi dan eksposur memakai semua
    posisi; aturan satu posisi per pair hanya memakai `same_source` (default: semua), mis. posisi dari sumber
    yang sama (strategi aturan vs setup AI)."""
    if len(open_positions) >= max_open:
        return f"maks {max_open} posisi terbuka tercapai"
    base, quote = split_currencies(symbol)
    pool = open_positions if same_source is None else same_source
    if any(split_currencies(s) == (base, quote) for s, _ in pool):
        return f"sudah ada posisi di {symbol}"
    exp = currency_exposure(open_positions + [(symbol, direction)])
    for cur in (base, quote):
        if abs(exp.get(cur, 0)) > max_exposure:
            return f"eksposur {cur} melebihi {max_exposure} posisi searah"
    return None


@dataclass
class DrawdownGuard:
    """Circuit breaker: berhenti entry jika equity turun >= max_drawdown dari puncak. Hanya reset manual."""
    max_drawdown: float
    peak: float = 0.0
    halted: bool = False

    def update(self, equity: float) -> bool:
        """Return True jika circuit breaker baru saja terpicu."""
        if self.halted:
            return False
        if equity > self.peak:
            self.peak = equity
            return False
        if self.peak and equity <= self.peak * (1 - self.max_drawdown):
            self.halted = True
            return True
        return False

    def drawdown(self, equity: float) -> float:
        return max(0.0, 1 - equity / self.peak) if self.peak else 0.0

    def reset(self, equity: float) -> None:
        self.peak, self.halted = equity, False


@dataclass
class DailyGuard:
    """Menghentikan entry baru jika equity turun >= max_daily_loss dari equity awal hari. Hari = tanggal `now`
    yang diberikan (bot live memakai WIB dan menyimpan equity awal hari di jurnal)."""
    max_daily_loss: float
    day: date | None = None
    start_equity: float = 0.0

    def update(self, now: datetime, equity: float) -> bool:
        """Return True jika hari baru dimulai."""
        if self.day != now.date():
            self.day, self.start_equity = now.date(), equity
            return True
        return False

    def pnl_pct(self, equity: float) -> float:
        return (equity - self.start_equity) / self.start_equity if self.start_equity else 0.0

    def halted(self, equity: float) -> bool:
        return self.pnl_pct(equity) <= -self.max_daily_loss
