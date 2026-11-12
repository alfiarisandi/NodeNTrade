# Fungsi bersama untuk script PowerShell bot (di-dot-source oleh run_forever.ps1 & start_all.ps1).
# Sengaja hanya ASCII: Windows PowerShell 5.1 salah membaca file UTF-8 tanpa BOM.

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$DataDir = Join-Path $Root "data"
$LogFile = Join-Path $DataDir "run_forever.log"
$StopFile = Join-Path $DataDir "STOP"
New-Item -ItemType Directory -Force $DataDir | Out-Null

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Read-DotEnv {
    $vals = @{}
    $file = Join-Path $Root ".env"
    if (Test-Path $file) {
        foreach ($line in Get-Content $file -Encoding UTF8) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
                $vals[$Matches[1]] = $Matches[2].Trim('"').Trim("'")
            }
        }
    }
    return $vals
}

function Send-Telegram([string]$Text) {
    $cfg = Read-DotEnv
    if (-not $cfg.TELEGRAM_BOT_TOKEN -or -not $cfg.TELEGRAM_CHAT_ID) { return }
    try {
        $body = @{ chat_id = $cfg.TELEGRAM_CHAT_ID; text = $Text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -TimeoutSec 15 -ContentType "application/json; charset=utf-8" `
            -Uri "https://api.telegram.org/bot$($cfg.TELEGRAM_BOT_TOKEN)/sendMessage" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
    } catch {
        # Jangan pernah menulis token ke log
        Write-Log ("Telegram gagal: " + ($_.Exception.Message -replace [regex]::Escape($cfg.TELEGRAM_BOT_TOKEN), "***"))
    }
}

function Test-Port([string]$HostName, [int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(2000)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-NineRouterUri {
    $cfg = Read-DotEnv
    if ($cfg.NINEROUTER_BASE_URL) { return [Uri]$cfg.NINEROUTER_BASE_URL }
    return [Uri]"http://localhost:20128"
}

function Get-BotProcesses {
    # Proses python yang menjalankan run_bot.py (launcher venv + interpreter = 2 proses untuk 1 bot)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*run_bot.py*" -and $_.CommandLine -notlike "*--check*" }
}

function Get-BotStatus {
    $cfg = Read-DotEnv
    $uri = Get-NineRouterUri
    [pscustomobject]@{
        "MT5 terminal jalan"    = [bool](Get-Process terminal64 -ErrorAction SilentlyContinue)
        "9Router jalan"         = (Test-Port $uri.Host $uri.Port)
        "Telegram terisi"       = [bool]($cfg.TELEGRAM_BOT_TOKEN -and $cfg.TELEGRAM_CHAT_ID)
        "Python venv ada"       = (Test-Path $Python)
        "Bot sedang jalan"      = [bool](Get-BotProcesses)
        "File STOP ada"         = (Test-Path $StopFile)
    }
}
