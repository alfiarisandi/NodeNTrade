# Riwayat pengerjaan

Catatan apa saja yang dikerjakan di bot ini, terbaru di atas. Rincian teknis ada di pesan commit
(`git log`), alasan angka-angka setelan ada di komentar `config/settings.yaml`.

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
