# Riwayat pengerjaan

Catatan apa saja yang dikerjakan di bot ini, terbaru di atas. Rincian teknis ada di pesan commit
(`git log`), alasan angka-angka setelan ada di komentar `config/settings.yaml`.

## 2026-11-16: Keputusan pemilik Tahap 5 (Commit E1–E4)

- **E1, H5:** `manage.partial_at_usd` 10 → 0, sehingga parsial $10 mati. Belum diganti dengan parsial berbasis R.
  Akan dievaluasi ulang setelah ada backtest kebijakan manajemen posisi setup. Parsial di +1R / TP1, breakeven
  1R, dan kunci 0,5R di 1,5R tetap seperti sebelumnya.
- **E2, H6:** Ichimoku H4 EURUSD masuk mode shadow (`symbols[].rule_mode: shadow`).
  - Sinyal dicatat dan di-shadow trade (`ai_decision = shadow_mode`), tanpa review AI dan tanpa order.
  - Posisi EURUSD yang sudah terbuka tetap dikelola (exit sinyal).
  - Setup 📊 Analisis AI di EURUSD tetap berjalan normal, dan Ichimoku XAUUSD tetap live.
  - 📋 Evaluasi menampilkan hasil shadow per pair. `ai_vs_rules` mencatatnya di kelompok `mode_shadow`.
- **Jam AI** tetap 15–23 WIB, dievaluasi setelah 2–4 minggu data screener 24/5.
- **Risiko per trade** tetap (0,5% dasar; Strong 0,5% / Moderate 0,25%).
- `tests/test_live_config.py` menjaga keputusan ini dan parameter yang dilindungi: Ichimoku H4 SL 2,5 ATR
  (exit sinyal, filter MA200), rugi harian 1,5%, dan risiko per trade.

## 2026-11-16: Remediasi audit, Commit D, backtest lebih realistis

Parameter strategi tidak diubah; variasi parameter hanya dilaporkan. Bot live tidak terpengaruh, karena perubahan
`symbol_meta` hanya menambah data swap.

**Perubahan:**
- **Swap broker (H7).**
  - `download_data.py --meta-only` menyimpan data swap broker (`swap_mode`, `swap_long`, `swap_short`,
    `swap_rollover3days`) ke `data/<SYMBOL>_meta.json`.
  - Engine menghitung swap per malam: long/short dibedakan, rollover 00:00 jam server, hari 3× sesuai broker
    (Rabu), dan tanpa rollover di akhir pekan. Mode yang didukung: point, bunga %, dan uang per lot.
  - R bersih sekarang = R kotor − komisi + swap. Ini berlaku untuk Ichimoku dan intraday.
- **Biaya eksekusi.** Spread + slippage dalam R (`exec_cost_r`), supaya stress test menggandakan seluruh biaya,
  bukan hanya komisi.
- **Entry terlambat.** Opsi `entry_delay` (N bar) untuk mensimulasikan jeda konfirmasi manual.
- **`backtest.py --robust`** → `reports/backtest_robustness.txt`. Isinya per pair, IS dan OOS terpisah: dasar,
  biaya ×2 / ×3, entry telat 1 bar, tanpa 1 / 3 trade terbaik, Monte Carlo 5.000×, sensitivitas jarak SL
  0,6–1,4× setelan, dan kriteria OOS. Kriteria OOS: PF > 1,2 dengan biaya ×2, expectancy > 0, DD ≤ 15R, dan
  tetap untung tanpa 2 trade terbaik.
- **Riset intraday per pair.** Kolom baru: swap, PF biaya ×2 / ×3, PF telat 1 bar, total tanpa 3 trade terbaik,
  dan peluang Monte Carlo total ≤ 0.

**Hasil (data s/d 2026-11-11, R bersih termasuk swap):**
- **Ichimoku H4 XAUUSD: LULUS kriteria OOS.**
  - PF IS / OOS 1,32 / 2,00. OOS dengan biaya ×2 PF 1,97; entry telat 1 bar PF 2,13.
  - Tanpa 3 trade terbaik masih +5,4R (PF 1,20).
  - Monte Carlo: P(total ≤ 0) 3,3%, DD p95 21,2R.
  - Sensitivitas SL: PF OOS 1,98–2,13. PF IS lemah di SL 1,5–2 ATR (1,05–1,10).
- **Ichimoku H4 EURUSD: TIDAK LULUS.**
  - PF IS / OOS 1,14 / 1,01 (+0,4R).
  - OOS dengan biaya ×2 PF 0,96; entry telat 1 bar PF 0,89; tanpa trade terbaik negatif.
  - Monte Carlo: P(total ≤ 0) 36,4%.
- **Intraday A–F: tidak ada yang lolos di kedua pair.** B di XAUUSD OOS PF 1,18 (biaya ×2 1,13), tetapi telat
  1 bar PF 0,69, hanya 54 trade, dan P(total ≤ 0) 25%.
- Keterbatasan: tarif swap sekarang dipakai untuk seluruh histori, data hanya OHLC tanpa tick, dan spread bar
  adalah spread minimum bar.

## 2026-11-16: Remediasi audit, Commit C, screener 24/5 catat-saja

Tujuannya mengukur berapa banyak peluang aturan yang sebenarnya tersedia, sebelum jam aktif AI diubah. Jam AI,
strategi, dan isi yang dikirim ke AI tidak berubah, jadi token tidak bertambah.

- **Pencatatan 24/5 (H8).**
  - Screener berjalan setiap candle M15 baru, Senin–Jumat 24 jam.
  - Semua kandidat aturan A–E dicatat ke `opportunities` dengan `kind = screener`, tanpa AI dan tanpa order.
  - Yang disimpan: waktu WIB (`time_wib`), pair, arah, strategi, regime, entry (≈ close), SL dan TP 1,5R menurut
    aturan riset, RR, biaya dalam R, dan alasan.
- **Nasib setiap kandidat:**
  - di luar jam AI;
  - ditolak regime (strategi tidak dipakai di regime itu);
  - pengulangan (pair + arah sudah dikirim ke AI < 60 menit, atau candle sudah dianalisis);
  - ditahan (pause, rugi harian, berita, kuota, batas analisis pemicu);
  - cooldown AI (baris yang sama diperbarui bila kemudian terkirim);
  - dikirim ke AI.
- **Deduplikasi:** kandidat dengan pair + arah + strategi sama dalam 60 menit ditautkan dan dihitung sekali.
- **Statistik di 📋 Evaluasi (🛰️ Screener 24/5):** kandidat per pair, strategi, regime, sesi, dan nasib. Juga
  berapa yang harganya tersentuh, kena TP 1,5R / SL / ambiguous, tidak pernah aktif, dan rata-rata R. Catatan
  screener tidak masuk statistik peluang AI dan tidak dikirim ke AI.
- **Replay data nyata 16/11 (00:00–12:30 WIB, tanpa AI):** 10 kandidat unik (6 pengulangan), semuanya SELL di
  regime TREND, sesi Asia, dan di luar jam AI.

## 2026-11-16: Remediasi audit, Commit B, integritas pengukuran

Tidak mengubah keputusan trading. Satu perilaku baru yang terlihat: setup yang sama tidak ditawarkan ulang.

- **Shadow setup AI (H4).**
  - Setup yang tidak dieksekusi (ditolak, tidak dijawab, diblok, gagal) kini dinilai dari candle M15.
  - Entry hanya dianggap terjadi bila zona tersentuh dalam `valid_minutes`.
  - Tidak lagi memakai bar H4 dan exit Ichimoku, dan biaya ikut dihitung.
  - Hasil lama tidak ditulis ulang. Baris setup dengan `shadow_method` NULL dikecualikan dari `ai_vs_rules`
    karena bias.
  - Verifikasi pada salinan jurnal 12/11: 4 shadow XAUUSD "TP +21,6R" menjadi `not_reached`.
- **Simulasi (M1).**
  - SL dan TP di candle yang sama, atau TP di candle pengisian, berstatus AMBIGUOUS dan tidak ditebak.
  - TP2 dinilai setelah TP1 (dengan SL awal).
  - Batas entry mengikuti `valid_minutes` setup.
- **Deduplikasi (M7).**
  - Setup dengan pair, arah, dan zona yang tumpang tindih dengan setup yang sudah ditawarkan dan masih berlaku
    tidak ditawarkan ulang.
  - Pengulangan ide (termasuk WAIT yang diulang tiap jam) ditautkan lewat `duplicate_of` dan dihitung satu kali.
- **Model AI (M8)** dicatat di setiap baris peluang (`opportunities.model`) dan sinyal (`signals.ai_model`).
- **Corong keputusan di 📋 Evaluasi.** Setiap ide dicatat tahap terjauhnya: AI WAIT, skor Weak / confidence
  Low, gerbang kode, manajemen risiko, ditawarkan, ditolak / tidak dijawab pemilik, diblok saat eksekusi, gagal
  eksekusi, limit tidak terisi, atau dieksekusi. Ditambah daftar tahap tempat peluang menang paling banyak hilang.

## 2026-11-16: Remediasi audit, Commit A, keamanan eksekusi (branch `fitur/remediasi-audit`)

Acuan perubahan ini adalah [AUDIT-2026-11-16.md](AUDIT-2026-11-16.md). Strategi, parameter entry, dan risiko per
trade tidak diubah.

- **Satu instance per mesin (C1).** `run_bot.py --live/--dry-run` memegang kunci `data/bot.lock`. Kuncinya
  dilepas OS saat proses mati, jadi tidak ada kunci basi. Instance kedua keluar dengan kode 3, dan
  `run_forever.ps1` tidak me-restart kode itu.
- **Bot di PC lain (C1).** Terdeteksi lewat Telegram `getUpdates` HTTP 409 Conflict. Saat konflik:
  - Entry baru diblok dan ada satu peringatan 🚨.
  - Polling diperlambat menjadi 60 detik.
  - Entry pulih otomatis setelah 10 menit tanpa 409.
- **Batas rugi harian (H1).** Equity awal hari (WIB) disimpan di jurnal (`daily_day`, `daily_start_equity`),
  jadi restart tidak lagi mereset batasnya. Pergantian hari kini pukul 00:00 WIB (sebelumnya 07:00 WIB / UTC),
  sama dengan kuota setup.
- **Kedaluwarsa order limit (H2).**
  - Order limit setup memakai `ORDER_TIME_SPECIFIED`, sehingga server MT5 membatalkannya tepat waktu walau bot
    mati.
  - Bila broker tidak mendukung, bot memakai `ORDER_TIME_DAY` atau GTC, dan pesan Telegram memberi peringatan.
  - Monex-Demo mendukung mode ini (`expiration_mode` = 15).
- **Kesegaran data (M9).** Entry ditolak bila terminal tidak tersambung ke server, atau tick lebih tua dari
  60 detik saat pasar seharusnya buka. Pasar tutup (akhir pekan, jeda harian logam) dibedakan dari feed yang
  terputus, dan alasannya tercatat di jurnal peluang.
- **Offset jam server dideteksi ulang tiap jam saat pasar buka**, kecuali offset diisi manual di setelan. Tanpa
  ini, bot yang start di akhir pekan dengan cadangan GMT+2 akan salah menghitung umur tick 1 jam saat server
  GMT+3 (DST), dan semua entry terblok.

## 2026-11-16: Deteksi peluang adaptif (branch `fitur/deteksi-adaptif`)

**Latar belakang:** analisis AI terlalu sering menjawab WAIT, karena semua konfirmasi opsional diperlakukan
seperti syarat wajib. Pemilik meminta pendekatan adaptif: semua peluang dideteksi dan dilaporkan, dan yang
dieksekusi hanya peluang yang lolos gerbang wajib.

- **Dua tahap di prompt AI.**
  - Tahap A (deteksi): semua peluang di tiga regime. TREND memakai pullback, EMA, ADX dan MSS. REVERSAL memakai
    sweep, MSS, FVG dan order block. RANGE memakai batas range H1 (strategi baru **R**) atau mean reversion.
  - Tahap B (eksekusi): BUY / SELL / WAIT / NO TRADE.
  - Indikator sekarang bukti pendukung. Konfirmasi opsional yang belum ada hanya mengurangi skor, tidak lagi
    membatalkan peluang.
- **Skor tiga tingkat:**
  - STRONG 8–10/10: risiko awal 0,5%.
  - MODERATE 6–7/10: risiko awal 0,25%.
  - WEAK di bawah 6: hanya dilaporkan.
  - Tombol risiko di atas tingkatnya disembunyikan. Opsi 1% tidak lagi muncul untuk setup AI.
- **Komponen skor baru:** context 25, structure 20, liquidity 15, price_action 20, location 15, rr 5.
  Arah H4/H1, susunan EMA dan ADX digabung jadi satu komponen "context" supaya indikator yang berkorelasi tidak
  dihitung dua kali.
- **RR bersih ≥ 1,3** setelah spread dan komisi, menggantikan RR kotor ≥ 1,5.
- **EV:**
  - Selama sebuah strategi belum punya 20 trade forward, labelnya "EV UNCALIBRATED", dan setup Moderate tetap
    boleh ditawarkan dengan risiko 0,25% sebagai uji forward.
  - Strategi yang EV forward-nya ≤ 0 setelah 20 trade otomatis tidak ditawarkan lagi.
- **Batas risiko:**
  - Rugi harian 1,5% (sebelumnya 2%).
  - Jeda setelah 3 rugi beruntun (sebelumnya 2).
  - Hanya 1 posisi atau limit setup AI aktif sekaligus. Posisi Ichimoku H4 tidak dihitung.
- **Jurnal peluang (tabel `opportunities`):**
  - Setiap setup dan peluang WAIT / NO_TRADE dicatat beserta skor, konfirmasi yang belum ada, zona, pembatal,
    RR, alasan, dan kondisi aktivasi.
  - Hasilnya dinilai dari candle M15 berikutnya (simulasi kasar): peluang terlewat yang ternyata menang, rugi
    yang terhindar, dan false positive.
- **📋 Evaluasi:** sekarang juga menampilkan EV forward per strategi dan statistik peluang 14 hari: jumlah
  terdeteksi, ditawarkan, ditolak, alasan tidak trade, serta perbandingan R yang ditawarkan vs yang disaring.
  Ringkasan yang sama dikirim ke AI supaya bisa menilai apakah filternya terlalu ketat atau terlalu longgar.
- **Pesan 📊 Analisis:** ditambah daftar "🔍 Peluang tercatat".
- Pasangan yang dianalisis tetap XAUUSD dan EURUSD, sesuai keputusan pemilik.

## 2026-11-16: Diagnosis "tidak ada posisi hari ini" (Senin pagi)

- Pada pukul 10:21 WIB tidak ada posisi, order, maupun deal sejak Jumat (equity 10.044,63).
- Penyebab: di branch `fitur/pantauan-screener` analisis AI otomatis, screener M15 dan pantauan pemicu baru aktif
  pukul 15:00–23:59 WIB. Pagi itu Ichimoku H4 juga tidak memberi sinyal.
- Screener sebenarnya sudah menemukan kandidat SELL (strategi A dan C, regime TREND) di kedua pair sejak pukul
  06:45, tapi kandidat itu tidak dikirim ke AI karena di luar jam aktif.
- Catatan: bot sekarang berjalan di PC lain. Task `TradingBot` di laptop ini masih aktif saat login, jadi ada
  risiko dua bot berjalan di satu akun.

## 2026-11-14: GitHub dan AGENTS.md

- Repo diunggah ke https://github.com/alfiarisandi/NodeNTrade. Branch `master` diganti nama jadi `main`.
  Riwayat sudah dicek: tidak ada `.env`, token, atau password yang ikut ter-commit.
- `AGENTS.md`: panduan untuk agen AI. Isinya aturan akun demo, rahasia `.env`, prosedur restart, perintah,
  peta kode, dan konvensi.

## 2026-11-13: Pantauan pemicu, screener, dan uji setelan kemarin

**Analisis hari itu tidak menghasilkan sinyal,** karena:
- Skor AI 45–61, di bawah ambang 65, saat H4 dan H1 bertabrakan.
- Filter berita 4 jam memblok sesi London.
- Laptop masuk standby.

**Perbaikan di branch `fitur/pantauan-screener`:**
- **Pantauan pemicu:** jawaban WAIT kini memuat skenario konkret ("close M15 di bawah X"). Bot mengecek setiap
  candle M15 dan memanggil AI lagi saat pemicunya kena.
- **Screener M15:** tiap candle M15 bot menjalankan aturan A–E tanpa AI. Bila ada kandidat, AI diminta menilai
  (maksimal 12 analisis tambahan per hari).
- **Analisis otomatis tiap jam** pukul 15–23 WIB.
- **Blok berita khusus setup AI** dipersingkat menjadi 1 jam sebelum s/d 0,5 jam sesudah event. Ichimoku tetap
  4 jam / 1 jam.
- **Cegah laptop tidur** (`SetThreadExecutionState`), ditambah notifikasi 💤 bila bot sempat berhenti.

**Riset intraday (commit 04a9892):**
- Lima strategi M15 (A EMA+ADX, B SMC, C VWAP, D ORB, E mean reversion) dan hybrid F diuji. Tidak ada yang lolos
  gerbang (PF > 1,2 di kedua periode).
- Studi win rate 80%: hanya bisa dicapai dengan TP kecil, dan hasilnya rugi.
- Hasil riset dipakai sebagai snapshot untuk AI (regime, VWAP, sweep, FVG, order block) dan sebagai bobot skor.

**Perubahan lain di commit yang sama:** tutup separuh posisi di profit $10, EURUSD maksimal 0,1 lot, dan laporan
candle H1.

**Branch `uji/setelan-kemarin`:**
- Mengembalikan prompt, skor (ambang 70/80) dan setelan ke versi 12/11 yang menghasilkan profit. Hasil 12/11:
  4 trade, +$44,78, hampir semuanya dari satu trade emas +$38,35.
- Perintah dan tombol Telegram tetap versi terbaru.

## 2026-11-12: Fondasi bot

- **Bot MT5 + Claude (9Router):**
  - Strategi Ichimoku H4, dipilih dari 20 strategi yang dibacktest.
  - Review sinyal oleh Claude (approve / reduce / reject).
  - Manajemen risiko: batas harian, circuit breaker, eksposur mata uang.
  - Jurnal SQLite, laporan harian dan mingguan, auto-restart lewat Task Scheduler.
- **Telegram:** menu tombol (Status, Posisi, P/L, Sinyal, Pause/Resume, Bantuan), dan konfirmasi manual dengan
  pilihan risiko sebelum order dikirim.
- **📊 Analisis:**
  - Claude menyusun market overview dan maksimal 2 setup per pair.
  - Konteks yang diberikan: struktur pasar, level kunci, dan sesi.
  - Setup dieksekusi sebagai order limit di tengah zona bila harga belum sampai.
- **Pengelolaan posisi setup:**
  - Dynamic SL dan partial TP.
  - Skor setup dan batas biaya (biaya maksimal 15% dari R).
  - Kuota 8 setup per hari dan jeda setelah 2 rugi beruntun.
- **🩺 Cek posisi oleh Claude:** HOLD / geser SL / tutup sebagian / tutup, plus saran untuk order limit.
- **📋 Evaluasi:** win rate, profit factor dan expectancy per pair dan sesi.
- **Model AI cadangan otomatis:** Sonnet, lalu model gratis big-pickle dan DeepSeek flash bila model utama kena
  limit.
