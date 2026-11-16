"""Notifikasi Telegram. Jika token/chat id kosong, pesan hanya ditulis ke log."""
from __future__ import annotations

import logging
import time

import requests

from bot.config import TelegramCfg

log = logging.getLogger(__name__)
MAX_LEN = 4000  # batas Telegram 4096 karakter per pesan
MAX_PENDING = 50
RETRY_SECONDS = 10
POLL_TIMEOUT = 8   # getUpdates yang macet jangan menahan loop bot terlalu lama
# HTTP 409 di getUpdates = instance lain memakai token bot yang sama (mis. bot di PC lain pada akun yang sama).
CONFLICT_RETRY_SECONDS = 60    # saat konflik polling diperlambat: jangan saling rebut update tiap 2 detik
CONFLICT_HOLD_SECONDS = 600    # tanpa 409 lagi selama ini = instance lain dianggap sudah berhenti


class Notifier:
    def __init__(self, cfg: TelegramCfg):
        self.cfg = cfg
        self.enabled = bool(cfg.token and cfg.chat_id)
        self.offset = 0
        self.pending: list[str] = []
        self.retry_at = 0.0
        self.down = False
        self.last_error_code: int | None = None
        self.conflict_at: float | None = None   # waktu (monotonic) 409 terakhir

    @property
    def conflict_active(self) -> bool:
        return self.conflict_at is not None and time.monotonic() - self.conflict_at < CONFLICT_HOLD_SECONDS

    def _safe(self, err: object) -> str:
        """Pesan error tanpa token bot (URL request memuat token)."""
        text = str(err)
        return text.replace(self.cfg.token, "***") if self.cfg.token else text

    def _call(self, method: str, payload: dict, timeout: float = 15, quiet: bool = False) -> dict | None:
        self.last_error_code = None
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.cfg.token}/{method}", json=payload,
                              timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            (log.debug if quiet else log.warning)("Telegram %s error: %s", method, self._safe(e))
            return None
        if not data.get("ok"):
            self.last_error_code = data.get("error_code")
            log.warning("Telegram %s gagal: %s", method, self._safe(data)[:200])
            return None
        return data.get("result")

    def send_buttons(self, text: str, buttons: list[list[tuple[str, str]]]) -> int | None:
        """Kirim pesan dengan inline keyboard [(label, callback_data)]. Return message_id."""
        log.info("NOTIF+TOMBOL: %s", text.replace("\n", " | "))
        if not self.enabled:
            return None
        res = self._call("sendMessage", {
            "chat_id": self.cfg.chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                 for row in buttons]}})
        return res["message_id"] if res else None

    def send_keyboard(self, text: str, rows: list[list[str]]) -> bool:
        """Kirim pesan + keyboard menu permanen di bawah kolom chat. Klik tombol = bot menerima teks labelnya."""
        log.info("NOTIF+MENU: %s", text.replace("\n", " | "))
        if not self.enabled:
            return False
        self._flush()
        if not self.pending and self._call("sendMessage", {
                "chat_id": self.cfg.chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True,
                "reply_markup": {"keyboard": [[{"text": t} for t in row] for row in rows],
                                 "resize_keyboard": True, "is_persistent": True}}) is not None:
            return True
        if len(self.pending) < MAX_PENDING:   # gagal: kirim ulang tanpa keyboard, isi pesan jangan hilang
            self.pending.append(text)
        return False

    def clear_buttons(self, message_id: int) -> None:
        if self.enabled and message_id:
            self._call("editMessageReplyMarkup", {"chat_id": self.cfg.chat_id, "message_id": message_id,
                                                  "reply_markup": {"inline_keyboard": []}})

    def answer(self, callback_id: str, text: str = "") -> None:
        if self.enabled:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Menu perintah di Telegram (tombol / di kolom chat)."""
        if self.enabled:
            self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def updates(self) -> list[dict]:
        """Update baru dari Telegram (non-blocking). Offset maju supaya tiap update hanya diproses sekali."""
        if not self.enabled or time.monotonic() < self.retry_at:
            return []
        res = self._call("getUpdates", {"offset": self.offset, "timeout": 0,
                                        "allowed_updates": ["callback_query", "message"]},
                         timeout=POLL_TIMEOUT, quiet=self.down)   # gangguan panjang: cukup 1 warning
        if res is None and self.last_error_code == 409:
            self.conflict_at = time.monotonic()
            self.retry_at = time.monotonic() + CONFLICT_RETRY_SECONDS
            return []
        if res is None:
            self.down = True
            self.retry_at = time.monotonic() + RETRY_SECONDS   # internet putus: jangan coba tiap 2 detik
            return []
        if self.down:
            self.down = False
            log.info("Koneksi Telegram pulih")
        if res:
            self.offset = res[-1]["update_id"] + 1
        self._flush()   # koneksi hidup: kirim ulang notif yang tertunda
        return res

    def _post_text(self, text: str) -> bool:
        ok = True
        for i in range(0, len(text), MAX_LEN):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{self.cfg.token}/sendMessage",
                    json={"chat_id": self.cfg.chat_id, "text": text[i:i + MAX_LEN],
                          "disable_web_page_preview": True},
                    timeout=15,
                )
                ok = ok and r.ok
                if not r.ok:
                    log.warning("Telegram gagal: %s %s", r.status_code, self._safe(r.text)[:200])
            except requests.RequestException as e:
                log.warning("Telegram error: %s", self._safe(e))
                ok = False
        return ok

    def _flush(self) -> None:
        while self.pending and self._post_text(self.pending[0]):
            self.pending.pop(0)

    def send(self, text: str) -> bool:
        log.info("NOTIF: %s", text.replace("\n", " | "))
        if not self.enabled:
            return False
        self._flush()
        if not self.pending and self._post_text(text):   # jaga urutan: antrean lama dulu
            return True
        if len(self.pending) < MAX_PENDING:   # gagal kirim: coba lagi nanti, jangan hilang
            self.pending.append(text)
        return False
