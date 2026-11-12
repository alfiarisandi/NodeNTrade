# Menjalankan Bot Terus-Menerus (Auto-Restart + Task Scheduler)

Bot perlu hidup **Senin–Jumat** selama pasar forex buka. Tiga hal harus menyala bersamaan:
terminal **MT5**, **9Router**, dan **bot**. Script di folder `scripts\` mengurus semuanya:

| Script | Fungsi |
|---|---|
| `start_all.ps1` | Menyalakan MT5 → 9Router → bot (dipakai Task Scheduler saat login) |
| `run_forever.ps1` | Menjalankan bot dan me-restart otomatis bila crash, plus notifikasi Telegram |
| `stop_bot.ps1` | Menghentikan bot tanpa di-restart |
| `install_task.ps1` | Memasang / menghapus task di Windows Task Scheduler |

Posisi terbuka tetap aman walau bot mati: **SL tersimpan di server broker**.

---

## 1. Cegah laptop sleep saat dicolok listrik

Jalankan di PowerShell (cukup sekali):

```powershell
powercfg /change standby-timeout-ac 0        # tidak pernah sleep saat dicolok
powercfg /change hibernate-timeout-ac 0      # tidak pernah hibernate saat dicolok
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0   # tutup lid = tidak melakukan apa-apa
powercfg /setactive SCHEME_CURRENT
```

Layar boleh mati (`monitor-timeout-ac`), itu tidak mengganggu bot.

**Windows Update:** buka *Settings > Windows Update > Advanced options > Active hours*, set
misalnya 06:00–02:00 supaya restart otomatis tidak terjadi saat pasar ramai.

## 2. Siapkan MT5 supaya bisa start sendiri

1. Saat login akun demo di MT5, centang **Save password**, supaya terminal login otomatis ketika dibuka.
2. *Tools > Options > Expert Advisors*:
   - centang **Allow algorithmic trading**
   - **hapus** centang *Disable algorithmic trading when the account has been changed*
   - **hapus** centang *Disable algorithmic trading when the profile has been changed*
3. Pastikan tombol **Algo Trading** di toolbar hijau. MT5 mengingat statusnya setelah ditutup.

Kalau path MT5 berbeda dari `C:\Program Files\MetaTrader 5\terminal64.exe`, isi `MT5_PATH` di `.env`.

## 3. 9Router hanya untuk laptop ini

Secara default 9Router listen di `0.0.0.0` (semua jaringan), sehingga perangkat lain di WiFi yang sama
bisa memakainya. `start_all.ps1` menjalankannya dengan:

```
9router --tray --no-browser --skip-update --host 127.0.0.1
```

Kalau 9Router sudah terlanjur jalan dengan setting default, tutup dulu (ikon tray → Quit), lalu
biarkan `start_all.ps1` yang menyalakannya. Perintah lain bisa diatur lewat `NINEROUTER_START_CMD` di `.env`.

## 4. Pasang Task Scheduler

1. **Hentikan bot yang sedang jalan manual** (Ctrl+C di jendelanya). Satu akun hanya boleh dijalankan 1 bot.
2. Pasang task:
   ```powershell
   cd D:\ME\APP\trading-bot
   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
   ```
3. Jalankan sekarang tanpa perlu logout:
   ```powershell
   Start-ScheduledTask -TaskName TradingBot
   ```
   Jendela PowerShell bot akan terbuka, dan Telegram menerima "🤖 Bot trading mulai".

Mulai sekarang, setiap kali Anda login ke Windows, task menunggu 1 menit lalu menyalakan
MT5 → 9Router → bot.

> Task berjalan di sesi login Anda (MT5 butuh desktop). Kalau laptop restart dan tidak ada yang login,
> bot tidak jalan. Login otomatis Windows (`netplwiz`) bisa mengatasinya, tapi membuat laptop bisa
> dibuka siapa saja. Pertimbangkan sebelum mengaktifkannya.

## 5. Cara kerja auto-restart

- Bot **crash / error saat start** → restart dengan jeda 30 detik, 1 menit, 2 menit, lalu tiap 5 menit.
  Setiap restart dikirim ke Telegram.
- Bot yang sempat jalan lebih dari 10 menit dianggap sehat, jadi jeda restart kembali ke 30 detik.
- **MT5 atau 9Router mati** → run_forever menunggu (cek tiap 30 detik) dan mengirim peringatan
  ke Telegram jika sudah 10 menit.
- **Ctrl+C di jendela bot** = berhenti normal, tidak di-restart.
- run_forever **menolak start** jika bot lain sudah berjalan (mencegah 2 bot di 1 akun).

## 6. Cek status, log, dan menghentikan

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1 -Check   # status semua komponen
Get-Content data\run_forever.log -Tail 20                                 # log auto-restart
Get-Content data\bot.log -Tail 20                                         # log bot
powershell -ExecutionPolicy Bypass -File scripts\stop_bot.ps1             # hentikan bot
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove # hapus task
```

Setelah `stop_bot.ps1`, jalankan lagi dengan `Start-ScheduledTask -TaskName TradingBot`.
