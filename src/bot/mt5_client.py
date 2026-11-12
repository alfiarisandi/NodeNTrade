"""Wrapper tipis di atas library MetaTrader5. Semua akses ke terminal MT5 lewat sini."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from bot.config import MT5Cfg

log = logging.getLogger(__name__)

TIMEFRAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
              "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1"}
RETRY_CODES = {10004, 10020, 10021}  # requote, price changed, price off


class MT5Error(RuntimeError):
    pass


class MT5Client:
    def __init__(self, cfg: MT5Cfg):
        import MetaTrader5 as mt5  # import di sini supaya backtest/test tidak butuh terminal
        self.mt5 = mt5
        self.cfg = cfg

    # ---------- koneksi ----------
    def connect(self) -> None:
        kwargs = {"timeout": 60_000}
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if not self.mt5.initialize(**kwargs):
            raise MT5Error(f"Gagal konek ke MT5: {self.mt5.last_error()}")
        # Login hanya jika terminal belum memakai akun ini: login ulang dianggap "ganti akun"
        # dan membuat MT5 mematikan Algo Trading (opsi bawaan terminal)
        acc = self.mt5.account_info()
        if self.cfg.login and (acc is None or acc.login != self.cfg.login):
            if not self.mt5.login(self.cfg.login, password=self.cfg.password, server=self.cfg.server):
                raise MT5Error(f"Login MT5 gagal: {self.mt5.last_error()}")

    def trading_enabled(self) -> bool:
        """Tombol Algo Trading di terminal menyala."""
        info = self.mt5.terminal_info()
        return bool(info and info.trade_allowed)

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def account(self):
        acc = self.mt5.account_info()
        if acc is None:
            raise MT5Error(f"account_info gagal: {self.mt5.last_error()}")
        return acc

    def is_demo(self) -> bool:
        return self.account().trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    def ensure_demo(self) -> None:
        """Pengaman utama: bot ini hanya boleh jalan di akun DEMO."""
        if not self.is_demo():
            acc = self.account()
            raise MT5Error(f"Akun {acc.login} di {acc.server} BUKAN akun demo. Bot dihentikan demi keamanan.")

    def server_utc_offset(self, probe_symbol: str) -> int | None:
        """Selisih jam server terhadap UTC, dari waktu tick terakhir. None jika pasar sedang tutup."""
        tick = self.mt5.symbol_info_tick(probe_symbol)
        if tick is None:
            return None
        diff_h = (tick.time - time.time()) / 3600
        offset = round(diff_h)
        # Tick basi (akhir pekan) membuat selisih tidak mendekati bilangan bulat jam / di luar rentang wajar
        if abs(diff_h - offset) > 0.1 or not -12 <= offset <= 14:
            return None
        return offset

    # ---------- simbol & data ----------
    def symbol_meta(self, symbol: str) -> dict:
        if not self.mt5.symbol_select(symbol, True):
            raise MT5Error(f"Simbol {symbol} tidak tersedia: {self.mt5.last_error()}")
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"symbol_info {symbol} gagal")
        return {
            "symbol": symbol,
            "point": info.point,
            "digits": info.digits,
            "tick_size": info.trade_tick_size,
            "tick_value": info.trade_tick_value,
            "contract_size": info.trade_contract_size,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "stops_level": info.trade_stops_level,
            "filling_mode": info.filling_mode,
            "currency_profit": info.currency_profit,
        }

    def find_symbols(self, pattern: str) -> list[str]:
        found = self.mt5.symbols_get(group=f"*{pattern}*") or ()
        return [s.name for s in found]

    def tick(self, symbol: str):
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise MT5Error(f"Tick {symbol} tidak tersedia")
        return t

    def _tf(self, tf: str) -> int:
        return getattr(self.mt5, TIMEFRAMES[tf])

    @staticmethod
    def _to_frame(rates) -> pd.DataFrame:
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume", "spread"])
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")  # jam server broker
        return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]

    def rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """`count` bar terakhir yang SUDAH close (bar berjalan tidak ikut)."""
        return self._to_frame(self.mt5.copy_rates_from_pos(symbol, self._tf(tf), 1, count))

    def rates_range(self, symbol: str, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
        start = start.replace(tzinfo=timezone.utc)
        end = end.replace(tzinfo=timezone.utc)
        df = self._to_frame(None)
        for _ in range(5):  # terminal kadang perlu waktu untuk mengunduh histori
            df = self._to_frame(self.mt5.copy_rates_range(symbol, self._tf(tf), start, end))
            if len(df):
                break
            time.sleep(2)
        return df

    # ---------- posisi & order ----------
    def positions(self, magic: int) -> list:
        return [p for p in (self.mt5.positions_get() or ()) if p.magic == magic]

    def _filling(self, symbol: str) -> int:
        mode = self.mt5.symbol_info(symbol).filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def market_order(self, symbol: str, direction: int, volume: float, sl_distance: float,
                     tp_distance: float, magic: int, comment: str):
        """Order market dengan SL & TP. Return hasil order_send yang sukses, atau raise MT5Error."""
        meta = self.symbol_meta(symbol)
        min_dist = meta["stops_level"] * meta["point"]
        if sl_distance <= min_dist or (tp_distance and tp_distance <= min_dist):
            raise MT5Error(f"Jarak SL/TP lebih kecil dari stops level broker ({min_dist})")
        last = None
        for _ in range(3):
            t = self.tick(symbol)
            price = t.ask if direction == 1 else t.bid
            req = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": self.mt5.ORDER_TYPE_BUY if direction == 1 else self.mt5.ORDER_TYPE_SELL,
                "price": price,
                "sl": round(price - direction * sl_distance, meta["digits"]),
                "tp": round(price + direction * tp_distance, meta["digits"]) if tp_distance else 0.0,
                "deviation": 20,
                "magic": magic,
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(symbol),
            }
            last = self.mt5.order_send(req)
            if last is not None and last.retcode == self.mt5.TRADE_RETCODE_DONE:
                return last
            if last is None or last.retcode not in RETRY_CODES:
                break
            time.sleep(1)
        detail = f"{last.retcode} {last.comment}" if last is not None else str(self.mt5.last_error())
        raise MT5Error(f"Order {symbol} gagal: {detail}")

    def close_position(self, position, comment: str = "bot exit") -> bool:
        """Tutup posisi dengan order market berlawanan."""
        is_buy = position.type == self.mt5.POSITION_TYPE_BUY
        last = None
        for _ in range(3):
            t = self.tick(position.symbol)
            last = self.mt5.order_send({
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
                "position": position.ticket,
                "price": t.bid if is_buy else t.ask,
                "deviation": 20,
                "magic": position.magic,
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(position.symbol),
            })
            if last is not None and last.retcode == self.mt5.TRADE_RETCODE_DONE:
                return True
            if last is None or last.retcode not in RETRY_CODES:
                break
            time.sleep(1)
        log.warning("Gagal tutup posisi %s: %s", position.ticket,
                    last.comment if last is not None else self.mt5.last_error())
        return False

    def modify_sl(self, position, new_sl: float) -> bool:
        digits = self.mt5.symbol_info(position.symbol).digits
        res = self.mt5.order_send({
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": round(new_sl, digits),
            "tp": position.tp,
        })
        ok = res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
        if not ok:
            log.warning("Gagal ubah SL %s: %s", position.ticket, res.comment if res else self.mt5.last_error())
        return ok

    def closed_position_result(self, ticket: int) -> dict | None:
        """Ringkasan posisi yang sudah ditutup: harga & waktu keluar, profit bersih (termasuk komisi/swap)."""
        deals = self.mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        outs = [d for d in deals if d.entry == self.mt5.DEAL_ENTRY_OUT]
        if not outs:
            return None
        last = max(outs, key=lambda d: d.time)
        profit = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        return {"close_price": last.price, "close_time": datetime.fromtimestamp(last.time, timezone.utc),
                "profit": profit}
