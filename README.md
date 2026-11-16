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
.venv\Scripts\python scripts\download_data.py --meta-only     # data swap broker ke data/*_meta.json
.venv\Scripts\python scripts\backtest.py --robust             # + swap, biaya x2/x3, telat 1 bar, Monte Carlo
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

## Riset intraday multi-strategi (M15)

`scripts/intraday_research.py` menguji lima strategi intraday berbasis aturan di bar M15 dengan bias H4/H1, plus
hybrid yang memilih strategi sesuai regime (`src/bot/intraday.py`):

| Kode | Strategi |
|---|---|
| A | EMA 20/50/200 + ADX + price action (pullback) |
| B | Smart Money Concepts (sweep → displacement + BOS/CHoCH → retrace ke FVG / order block) |
| C | VWAP sesi + EMA |
| D | Opening range breakout (range Asia, pembukaan London, pembukaan New York) + retest |
| E | Mean reversion (Bollinger + RSI, hanya di regime RANGE) |
| F | Hybrid: A–D di TREND, E (+B) di RANGE, tidak trade di MIXED, ukuran separuh saat volatilitas tinggi |

```powershell
.venv\Scripts\python scripts\download_data.py --from 2019-01-01 --tf M5 M15 H1 H4 D1 --symbols EURUSD XAUUSD
.venv\Scripts\python scripts\intraday_research.py            # semua studi -> reports/intraday_*.csv + intraday_summary.txt
.venv\Scripts\python scripts\intraday_research.py --study walkforward
```

Studi: `strategies` (A–F, in-sample vs out-of-sample), `regime` (per regime, sesi, volatilitas), `exits` (TP tetap,
parsial, breakeven, trailing ATR/EMA20/swing/VWAP), `score` (skor confluence vs hasil), `filters`, `walkforward`
(latih 12 bulan → uji 6 bulan), `m5` (entry M15 vs konfirmasi candle M5).

**Hasil 2026-11-13** (EURUSD + XAUUSD, M15 2022-08 s/d 2026-11, setelah spread, slippage dan komisi $10/lot):
tidak ada strategi yang lolos gerbang (PF > 1.2, expectancy > 0, DD < 15%, ≥ 100 trade, kedua pair PF > 1).

- A–F dengan TP 1.5R: PF 0.74–0.93 di in-sample dan 0.72–1.11 di out-of-sample; win rate 34–45%, jauh dari
  sasaran 55–70%. Hanya B yang positif di out-of-sample (PF 1.11, 122 trade), tapi negatif di in-sample.
- Biaya (komisi ~0.05R + spread dan slippage) memakan sekitar 0.1R per trade di M15, jadi strategi tanpa edge
  berakhir sekitar −0.12R/trade. Konfirmasi candle M5 tidak mengubah tanda hasil.
- Skor confluence tidak berkorelasi dengan hasil (Spearman −0.02 in-sample, 0.00 out-of-sample).
- Kebijakan exit tidak mengubah tanda: parsial di 1R menaikkan win rate ke ~46–51% dengan PF tetap < 1.
- Walk-forward: PF 1.19, +22.6R dari 201 trade, 4 dari 7 jendela positif → gagal tipis. Empat jendela terakhir
  memilih D dengan TP 2R dan skor ≥ 65 (16–42 trade per jendela): kandidat untuk dipantau, bukan bukti edge.

**Studi win rate 80% (`--study winrate`, 2026-11-13).** 240 konfigurasi (A–F × TP 0.2–1R × 4 lebar SL × 2 filter
sesi) dibanding entry acak: tidak ada yang punya win rate ≥ 80% dengan expectancy > 0 di kedua periode.

- Win rate 80% bisa didapat tanpa edge: entry acak dengan TP 0.2R menang 78–82%, tetapi butuh ~86–89% untuk
  impas, sehingga rugi (PF 0.44–0.76).
- Di geometri yang sama, strategi A–F menang 80–82%, praktis sama dengan entry acak (81%).
- Dengan TP 0.5R entry acak menang ~64% dan impas di ~70%; mencapai 80% di sana butuh edge +16 poin, sedangkan
  strategi yang ada umumnya dalam ±3 poin dari acak (terbaik: D, +5 poin di out-of-sample saja).
- Walk-forward yang memilih konfigurasi win rate ≥ 80%: win rate uji 81.9% dengan PF 0.69 (−9R dari 155 trade).

**Win rate vs profit di strategi live (`variants.py --family wr [--oos]`, 2026-11-13).** Ichimoku H4 + MA200
adalah satu-satunya strategi yang untung di kedua periode, jadi di sinilah win rate bisa dinaikkan tanpa jadi rugi.
EURUSD + XAUUSD, in-sample 2019–2023 / out-of-sample 2024–2026:

| Exit | Win rate | PF | Total R |
|---|---|---|---|
| Live sekarang (tanpa TP, keluar lewat sinyal) | 36.9% / 41.2% | 1.31 / 1.57 | +22.4 / +32.2 |
| Parsial 50% di 1.5R, sisa lewat sinyal | 38.1% / 45.8% | 1.15 / 1.52 | +10.9 / +27.5 |
| Parsial 50% di 1R + breakeven | 43.8% / 51.9% | 1.15 / 1.38 | +10.2 / +18.4 |
| Parsial 50% di 0.5R + breakeven | 62.3% / 63.4% | 1.23 / 1.31 | +11.1 / +11.9 |
| TP penuh di 0.5R | 62.6% / 64.4% | 1.04 / 1.06 | +2.1 / +2.3 |

Win rate dan profit bergerak berlawanan: menaikkan win rate dari ~40% ke ~63% memotong profit sekitar 50–63%.
Tidak ada varian yang menaikkan keduanya sekaligus.

Keterbatasan: data M15 hanya ~4,3 tahun dan M5 ~16 bulan; spread bar MT5 adalah spread minimum bar; VWAP
memakai tick volume; regime berita tidak diuji (tidak ada kalender historis). Strategi aturan ini **tidak**
dipakai live. Yang dipakai live dari riset ini hanya snapshot `intraday` (regime, VWAP, sweep, FVG) dan bobot
skor untuk setup 📊 Analisis.

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
| `src/bot/intraday.py` | fitur M15 + bias H4/H1, struktur (sweep, BOS, FVG), regime, strategi intraday A–F |
| `src/bot/journal.py` | SQLite: sinyal, keputusan AI, shadow trade, trade, equity |

## Catatan

- Backtest hanya menguji lapisan teknikal; Claude tidak di-backtest karena model sudah "melihat" harga historis.
- Kriteria lanjut dari backtest ke demo: profit factor out-of-sample > 1.2, drawdown < 15%, ≥ 100 trade.
- Memakai langganan Claude Code (OAuth) lewat 9Router untuk bot otomatis berpotensi melanggar ketentuan
  Anthropic. Untuk pemakaian jangka panjang, ganti ke Anthropic API key (cukup ubah `.env`).
