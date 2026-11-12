<#
.SYNOPSIS
  Nyalakan semua yang dibutuhkan bot: terminal MT5, 9Router, lalu bot (lewat run_forever.ps1).
  Dipakai oleh Task Scheduler saat Windows login, tapi bisa juga dijalankan manual.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1 -Mode dry-run

.NOTES
  Opsional di .env:
    MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
    NINEROUTER_START_CMD=9router --tray --no-browser --skip-update --host 127.0.0.1
#>
param([ValidateSet("live", "dry-run")] [string]$Mode = "live")
. (Join-Path $PSScriptRoot "_common.ps1")
Set-Location $Root
$cfg = Read-DotEnv

# 1. Terminal MT5 (harus login otomatis: centang "Save password" saat login di MT5)
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    $mt5 = if ($cfg.MT5_PATH) { $cfg.MT5_PATH } else { "C:\Program Files\MetaTrader 5\terminal64.exe" }
    if (Test-Path $mt5) {
        Write-Log "Menyalakan MT5: $mt5"
        Start-Process -FilePath $mt5
        Start-Sleep -Seconds 30   # beri waktu terminal login ke server broker
    } else {
        Write-Log "MT5 tidak ditemukan di $mt5. Isi MT5_PATH di .env."
    }
}

# 2. 9Router (hanya untuk laptop ini: --host 127.0.0.1, bukan seluruh jaringan WiFi)
$uri = Get-NineRouterUri
if (-not (Test-Port $uri.Host $uri.Port)) {
    $cmd = if ($cfg.NINEROUTER_START_CMD) { $cfg.NINEROUTER_START_CMD }
           elseif (Get-Command 9router -ErrorAction SilentlyContinue) { "9router --tray --no-browser --skip-update --host 127.0.0.1" }
           else { $null }
    if ($cmd) {
        Write-Log "Menyalakan 9Router: $cmd"
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd -WindowStyle Minimized
        for ($i = 0; $i -lt 30 -and -not (Test-Port $uri.Host $uri.Port); $i++) { Start-Sleep -Seconds 2 }
    } else {
        Write-Log "9Router belum jalan dan perintah startnya tidak diketahui. Isi NINEROUTER_START_CMD di .env."
    }
}

# 3. Bot dengan auto-restart
& (Join-Path $PSScriptRoot "run_forever.ps1") -Mode $Mode
