<#
.SYNOPSIS
  Jalankan bot terus-menerus: restart otomatis bila crash, dengan notifikasi Telegram.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1              # mode --live (akun demo)
  powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1 -Mode dry-run
  powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1 -Check       # cek prasyarat saja

.NOTES
  - Bot berhenti normal (Ctrl+C di jendela ini) = tidak di-restart.
  - Bot crash / error saat start = restart dengan jeda 30s, 60s, 2m, lalu tiap 5m.
  - Hentikan dari luar: jalankan scripts\stop_bot.ps1 (membuat data\STOP lalu menutup bot).
  - Menunggu sampai terminal MT5 dan 9Router hidup sebelum menjalankan bot.
#>
param(
    [ValidateSet("live", "dry-run")] [string]$Mode = "live",
    [switch]$Check
)
. (Join-Path $PSScriptRoot "_common.ps1")
Set-Location $Root

if ($Check) {
    Get-BotStatus | Format-List
    exit 0
}

if (Get-BotProcesses) {
    Write-Log "Bot sudah berjalan di proses lain. Hentikan dulu (Ctrl+C / stop_bot.ps1) agar tidak ada 2 bot."
    exit 1
}
if (Test-Path $StopFile) { Remove-Item $StopFile -Force }

$Delays = @(30, 60, 120, 300)
$Failures = 0
$WaitingSince = $null
Write-Log "run_forever mulai (mode --$Mode)"

while ($true) {
    if (Test-Path $StopFile) {
        Write-Log "File STOP ditemukan, run_forever berhenti."
        Remove-Item $StopFile -Force
        break
    }

    $status = Get-BotStatus
    if (-not $status."MT5 terminal jalan" -or -not $status."9Router jalan") {
        if (-not $WaitingSince) { $WaitingSince = Get-Date }
        $mins = [int]((Get-Date) - $WaitingSince).TotalMinutes
        Write-Log ("Menunggu prasyarat: MT5={0} 9Router={1} ({2} menit)" -f $status."MT5 terminal jalan", $status."9Router jalan", $mins)
        if ($mins -eq 10) {
            Send-Telegram "Bot belum bisa start: terminal MT5 atau 9Router belum jalan selama 10 menit."
        }
        Start-Sleep -Seconds 30
        continue
    }
    $WaitingSince = $null

    $started = Get-Date
    Write-Log "Menjalankan bot: run_bot.py --$Mode"
    & $Python (Join-Path $Root "scripts\run_bot.py") "--$Mode"
    $code = $LASTEXITCODE
    $ranMinutes = ((Get-Date) - $started).TotalMinutes

    if ($code -eq 0) {
        Write-Log "Bot berhenti normal (exit 0). run_forever selesai."
        break
    }
    if ($code -eq 3) {   # bot.instance.EXIT_ALREADY_RUNNING: jangan restart, nanti dua bot di satu akun
        Write-Log "Bot lain sudah berjalan di mesin ini (exit 3). run_forever berhenti tanpa restart."
        Send-Telegram "Bot tidak dijalankan: instance lain sudah berjalan di mesin ini (kunci data\bot.lock)."
        break
    }
    if (Test-Path $StopFile) { continue }

    # Bot yang sempat jalan lama dianggap sehat: jeda restart kembali ke awal
    if ($ranMinutes -gt 10) { $Failures = 0 }
    $wait = $Delays[[Math]::Min($Failures, $Delays.Count - 1)]
    $Failures++
    Write-Log ("Bot berhenti dengan kode {0} setelah {1} menit. Restart ke-{2} dalam {3} detik." -f $code, [int]$ranMinutes, $Failures, $wait)
    Send-Telegram ("Bot berhenti tak terduga (kode {0}) setelah {1} menit. Restart otomatis ke-{2} dalam {3} detik. Cek data\bot.log" -f $code, [int]$ranMinutes, $Failures, $wait)
    Start-Sleep -Seconds $wait
}
