# Trading Bot Forex (MT5 + Python + Claude)

Bot trading untuk **akun demo** MetaTrader 5 (diuji di MIFX / Monex-Demo). Strategi teknikal mencari sinyal,
Claude (lewat 9Router) bertindak sebagai manajer risiko yang menyetujui, mengecilkan, atau menolak setiap sinyal.

Strategi aktif: **Ichimoku H4** (9/26/52), risiko 0.5% per trade. Dari 20 strategi yang diuji (trend following,
mean reversion, dan 5 strategi open source populer), ini satu-satunya yang untung di kedua periode backtest:
in-sample 2019–2023 PF 1.26, out-of-sample 2024–2026 PF 1.09 (DD -13.1%). Edge-nya lemah, jadi tahap
berikutnya adalah dry-run untuk mengumpulkan data forward.

> ⚠️ Bot menolak jalan di akun real. Hasil backtest/demo tidak menjamin profit.

## Alur keputusan

```
Candle H4 close -> exit sinyal: Tenkan memotong balik Kijun -> tutup posisi
                -> sinyal baru: Tenkan memotong Kijun di luar awan Ichimoku
                -> cek risiko (rugi harian 3%, maks 3 posisi, eksposur mata uang, spread)
                -> review Claude: approve (x1.0) / reduce (x0.25-0.75) / reject
                -> lot = 0.5% equity x multiplier, SL 2.5 ATR, tanpa TP (keluar lewat sinyal)
                -> jurnal SQLite + notifikasi Telegram
```
Sinyal yang ditolak AI (dan semua sinyal saat dry-run) tetap disimulasikan sebagai *shadow trade*,
sehingga `--export` bisa membandingkan "aturan saja" vs "aturan + Claude".

## Setup

1. **MT5 + akun demo.** Install MetaTrader 5 dari broker Anda (cek apakah MIFX menyediakan MT5; jika hanya MT4,
   pakai MT5 dari metaquotes.net lalu *File > Open an Account > MetaQuotes-Demo*). Di MT5: *Tools > Options >
   Expert Advisors* centang **Allow algorithmic trading**, dan *Charts > Max bars in chart* set ke `Unlimited`.
2. **Python env**
   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   copy .env.example .env   # lalu isi
   ```
3. **9Router**: jalankan 9Router, hubungkan provider Claude Code di dashboard, salin API key ke `.env`,
   dan isi `AI_MODEL` dengan nama model persis dari dashboard (contoh `cc/claude-opus-4-7`).
4. **Telegram** (opsional): buat bot lewat @BotFather, isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`.
   Saat bot mulai, tombol menu (Status, Posisi, P/L, Sinyal, Pause/Resume, Bantuan) muncul di bawah kolom chat.
   Jika tombolnya tersembunyi, kirim `/menu` atau pesan apa saja.

## Pemakaian (urut)

```powershell
.venv\Scripts\python scripts\run_bot.py --check          # akun DEMO? simbol, suffix, spread, lot
.venv\Scripts\python scripts\run_bot.py --check-llm      # Claude via 9Router tersambung?
.venv\Scripts\python scripts\download_data.py --from 2019-01-01
.venv\Scripts\python scripts\backtest.py --split 2024-01-01   # laporan + grafik di reports/
.venv\Scripts\python scripts\variants.py --family os           # bandingkan strategi (tf / mr / os)
.venv\Scripts\python scripts\run_bot.py --test-review EURUSD  # contoh review Claude, tanpa order
.venv\Scripts\python scripts\run_bot.py --dry-run        # beberapa minggu: catat sinyal tanpa order
.venv\Scripts\python scripts\run_bot.py --live           # order sungguhan di akun DEMO
.venv\Scripts\python scripts\run_bot.py --export         # jurnal ke CSV + AI vs aturan
.venv\Scripts\python scripts\run_bot.py --news           # kalender ekonomi & pair yang sedang diblok
.venv\Scripts\python -m pytest                           # unit test
```

Jika `--check` menampilkan simbol `TIDAK ADA`, isi `symbol_suffix` di `config/settings.yaml` sesuai alternatif
yang ditampilkan (misal `.m`). Sesuaikan juga `max_spread_points` per simbol dengan spread nyata broker.

## Profil akun kecil ($100)

`config/settings.small.yaml` mewarisi `settings.yaml` lalu menimpa beberapa bagian:
- hanya EURUSD,
- lot minimum 0.01 boleh dipakai selama rugi di SL ≤ 10% equity (`risk.min_lot_max_risk`),
- jurnal dan magic number terpisah,
- circuit breaker 50%.

Aktifkan lewat `--config` atau `BOT_CONFIG` di `.env`:

```powershell
.venv\Scripts\python scripts\run_bot.py --config config/settings.small.yaml --check   # kolom ModalMin
.venv\Scripts\python scripts\backtest.py --config config/settings.small.yaml --balance 100
```

Backtest akun $100 (lot nyata): 2019–2023 +31% (DD -40%), 2024–2026 **-47%** (DD -51%). Ini profil uji di
demo, bukan rekomendasi untuk uang sungguhan.

## Menjalankan terus-menerus

Auto-restart, start otomatis saat login (Task Scheduler), dan pencegahan sleep: lihat
[docs/AUTOSTART.md](docs/AUTOSTART.md).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1   # pasang sekali
Start-ScheduledTask -TaskName TradingBot                             # jalankan sekarang
```

## Struktur

| Path | Isi |
|---|---|
| `config/settings.yaml` | parameter strategi, risiko, sesi, AI, backtest |
| `src/bot/strategy.py` | indikator → sinyal; simulasi exit (dipakai backtest & live) |
| `src/bot/risk.py` | lot sizing 1%, batas harian, eksposur mata uang |
| `src/bot/ai/` | client 9Router, konteks pasar, reviewer (batas keras), laporan harian |
| `src/bot/live.py` | loop bot live |
| `src/bot/backtest/` | engine bar-by-bar + metrik & grafik |
| `src/bot/journal.py` | SQLite: sinyal, keputusan AI, shadow trade, trade, equity |

## Catatan

- Backtest hanya menguji lapisan teknikal; Claude tidak di-backtest karena model sudah "melihat" harga historis.
- Kriteria lanjut dari backtest ke demo: profit factor out-of-sample > 1.2, drawdown < 15%, ≥ 100 trade.
- Memakai langganan Claude Code (OAuth) lewat 9Router untuk bot otomatis berpotensi melanggar ketentuan
  Anthropic. Untuk pemakaian jangka panjang, ganti ke Anthropic API key (cukup ubah `.env`).
