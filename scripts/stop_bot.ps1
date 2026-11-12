<#
.SYNOPSIS
  Hentikan bot yang dijalankan run_forever.ps1 / Task Scheduler, tanpa di-restart otomatis.
  Posisi terbuka tetap aman: SL tersimpan di server broker.
#>
. (Join-Path $PSScriptRoot "_common.ps1")

New-Item -ItemType File -Force $StopFile | Out-Null   # run_forever berhenti setelah bot mati
$procs = Get-BotProcesses
if (-not $procs) {
    Write-Log "Tidak ada bot yang berjalan."
    exit 0
}
foreach ($p in $procs) {
    Write-Log "Menghentikan bot PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Send-Telegram "Bot dihentikan lewat stop_bot.ps1 (tidak akan restart otomatis)."
