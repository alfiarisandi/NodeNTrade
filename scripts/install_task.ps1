<#
.SYNOPSIS
  Daftarkan bot ke Windows Task Scheduler: start_all.ps1 jalan otomatis 1 menit setelah Anda login.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1               # pasang (mode --live)
  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Mode dry-run
  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove       # hapus
#>
param(
    [ValidateSet("live", "dry-run")] [string]$Mode = "live",
    [switch]$Remove
)
. (Join-Path $PSScriptRoot "_common.ps1")
$TaskName = "TradingBot"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' dihapus."
    exit 0
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$script = Join-Path $Root "scripts\start_all.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Mode $Mode"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT1M"   # tunggu 1 menit setelah login (jaringan & desktop siap)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
# Interactive: MT5 butuh desktop, jadi task jalan di sesi login Anda (bukan sebagai service)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Trading bot (MT5 demo): MT5 + 9Router + bot dengan auto-restart. Lihat docs\AUTOSTART.md" | Out-Null
Write-Host "Task '$TaskName' terpasang: start_all.ps1 -Mode $Mode, 1 menit setelah $user login."
Write-Host "Jalankan sekarang tanpa logout:  Start-ScheduledTask -TaskName $TaskName"
