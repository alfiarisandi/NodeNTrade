# AGENTS.md

Panduan untuk agen AI (Claude Code, Codex, dll.) yang bekerja di repo ini. Gambaran umum, setup dan hasil riset
ada di [README.md](README.md); auto-start Windows di [docs/AUTOSTART.md](docs/AUTOSTART.md).

## Ringkas

Bot trading forex/emas (EURUSD, XAUUSD) untuk **akun DEMO** MetaTrader 5 (Monex-Demo), ditulis dalam Python 3.12
dan berjalan di Windows. Ada dua jalur:

- **Ichimoku H4** (`src/bot/strategy.py`): sinyal otomatis, direview Claude (approve / reduce / reject).
- **📊 Analisis AI** (`src/bot/ai/analysis.py`, `prompts.py`): Claude membaca snapshot pasar M15/H1/H4 lalu
  menawarkan setup intraday dengan skor. Order hanya dibuka setelah pemilik mengonfirmasi lewat tombol Telegram.

Claude dipanggil lewat 9Router (endpoint kompatibel Anthropic, `src/bot/ai/client.py`), dengan model cadangan
otomatis. Bot dikendalikan dari Telegram (`src/bot/notifier.py`, handler `cmd_*` di `src/bot/live.py`). Semua
sinyal, keputusan AI, trade dan state disimpan di SQLite (`src/bot/journal.py`, file di `data/`).

## Aturan keras

- **Hanya akun demo.** Jangan melemahkan atau melewati cek `MT5Client.is_demo()` (`src/bot/mt5_client.py`).
  Jangan menambah jalur yang mengirim order ke akun real.
- **Jangan pernah menampilkan, mencetak, atau meng-commit isi `.env`** (login MT5, token Telegram, API key
  9Router). Variabel baru cukup ditambahkan sebagai contoh kosong di `.env.example`.
- `data/`, `reports/`, `*.db` dan `.venv/` tidak masuk git (lihat `.gitignore`).
- **Restart bot hanya bila tidak ada posisi atau order terbuka.** Cek dulu di MT5 atau lewat tombol Posisi.
  Urutannya:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\stop_bot.ps1
  Start-ScheduledTask -TaskName TradingBot
  # tunggu baris "Bot trading mulai" di data\bot.log
  ```
- Jangan restart atau ganti branch saat bot berjalan tanpa izin pemilik, karena bot membaca kode dari working tree.
- **Satu bot per akun.** `run_bot.py` memegang kunci `data/bot.lock`; instance kedua keluar dengan kode 3. Bot di
  PC lain terdeteksi lewat Telegram 409, dan entry baru diblok. Jangan menonaktifkan pengaman ini.
- Commit atas nama pemilik saja: **tanpa** trailer `Co-Authored-By: Claude`, tanpa tautan `Claude-Session:`, dan
  tanpa baris "Generated with Claude Code". Commit dan push hanya bila diminta.

## Perintah

```powershell
.venv\Scripts\python -m pytest                            # semua unit test (pytest.ini: pythonpath = src)
.venv\Scripts\python -m pytest tests\test_telegram.py -q  # satu file
.venv\Scripts\python scripts\run_bot.py --check           # akun DEMO? simbol, spread, lot
.venv\Scripts\python scripts\run_bot.py --check-llm       # 9Router tersambung?
.venv\Scripts\python scripts\run_bot.py --analysis        # satu kali 📊 Analisis (memakai token AI)
.venv\Scripts\python scripts\run_bot.py --news            # kalender & pair yang sedang diblok
.venv\Scripts\python scripts\backtest.py --split 2024-01-01
```

`--check`, `--check-llm`, `--analysis` dan `--test-review` membutuhkan MT5 yang sedang login dan/atau 9Router.
Unit test tidak butuh keduanya karena memakai fake (`FakeMT`, `FakeLLM`, `FakeNotifier` di `tests/`).

## Peta kode

| Path | Isi |
|---|---|
| `config/settings.yaml` | semua parameter: simbol, risiko, sesi, berita, AI (jam analisis, model, `max_tokens`), manajemen posisi |
| `src/bot/config.py` | model pydantic untuk settings; field baru harus punya default |
| `src/bot/live.py` | `LiveBot`: `run()` → `step()` tiap ~20 detik; perintah Telegram `cmd_*`; konfirmasi setup; manajemen order limit |
| `src/bot/ai/prompts.py` | system prompt: review sinyal, `ANALYSIS_SYSTEM`, review posisi, laporan harian |
| `src/bot/ai/analysis.py` | model `Setup`/`Opportunity`/`Analysis`, `SETUP_SCORE_WEIGHTS`, tingkat Strong/Moderate/Weak, `validate_setup` (batas keras RR bersih, SL, biaya) |
| `src/bot/opportunities.py` | simulasi hasil peluang dari candle M15 dan statistik filter (peluang terlewat, false positive, alasan tidak trade) |
| `src/bot/ai/context.py` | snapshot JSON yang dikirim ke Claude |
| `src/bot/intraday.py` | fitur M15, bias H4/H1, regime, sweep/BOS/FVG, strategi riset A–F |
| `src/bot/risk.py` | lot sizing, batas rugi harian, eksposur mata uang |
| `src/bot/news.py` | kalender ekonomi dan jendela blok berita |
| `src/bot/journal.py` | SQLite: sinyal, keputusan AI, shadow trade, trade, equity, key-value state |
| `scripts/` | CLI (`run_bot.py`, backtest, riset) dan skrip PowerShell untuk Task Scheduler |

## Konvensi

- Teks untuk pengguna (pesan Telegram, log, komentar, docstring, pesan commit) ditulis dalam **bahasa Indonesia**.
  Prompt ke Claude ditulis dalam bahasa Inggris.
- File repo memakai line ending **CRLF**. Pertahankan CRLF saat mengedit, terutama bila memakai regex atau skrip.
- Ikuti gaya kode sekitarnya: fungsi pendek, komentar seperlunya, tanpa abstraksi tambahan.
- Waktu:
  - Server MT5 = GMT+2/+3. Tampilan ke pemilik memakai WIB (UTC+7).
  - `mt5.rates()` sudah membuang bar yang masih terbentuk, jadi `iloc[-1]` adalah bar terakhir yang sudah close.
- Batas keras ada di kode (`validate_setup`, `risk.py`), bukan hanya di prompt. Aturan baru yang penting
  ditegakkan di kode, lalu diberi tes.
- Setiap perubahan perilaku disertai tes di `tests/`, dengan fake yang sudah ada. Jangan memanggil MT5 atau LLM
  sungguhan di tes.
- Skrip coba-coba yang memanggil MT5 atau AI sungguhan disimpan di luar repo, dan tidak boleh membuka order.

## Branch

Riwayat pengerjaan per tanggal: [docs/RIWAYAT.md](docs/RIWAYAT.md). Tambahkan entri baru di sana setiap ada perubahan
perilaku bot.

- `main`: baseline.
- `fitur/pantauan-screener`: watchlist pemicu AI, screener M15, blok berita setup 1 jam, cegah laptop tidur.
- `uji/setelan-kemarin`: uji prompt dan skor versi 12/11 (ambang 70/80, snapshot tanpa blok intraday), dengan
  perintah Telegram terbaru.
- `fitur/deteksi-adaptif` (dari `fitur/pantauan-screener`): deteksi dua tahap, skor Strong/Moderate/Weak dengan
  risiko 0,5% / 0,25%, RR bersih ≥ 1,3, EV forward per strategi, satu setup aktif, jurnal peluang.
