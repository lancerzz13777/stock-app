# Puts the stock bot on your free Oracle Cloud server (or updates it) in one go.
#   First time:   deploy_to_server.bat            (uploads code + your data + .env, installs, starts)
#   Code update:  deploy_to_server.bat -CodeOnly  (keeps the server's own data, wallet and .env)
# After a first deploy the PC stops running the bot (two copies would fight over Telegram).
param([string]$Ip, [string]$Key, [string]$User, [switch]$CodeOnly)
$ErrorActionPreference = "Stop"
$App = Split-Path $PSScriptRoot -Parent
$SettingsFile = Join-Path $App "deploy\server.txt"   # remembers IP + key path for next time

if (Test-Path $SettingsFile) {
    $saved = Get-Content $SettingsFile
    if (-not $Ip) { $Ip = $saved[0] }
    if (-not $Key) { $Key = $saved[1] }
    if (-not $User -and $saved.Count -gt 2) { $User = $saved[2] }
}
if (-not $User) { $User = "ubuntu" }   # Oracle's default; your own Ubuntu machine uses your login name
if (-not $Ip) { $Ip = Read-Host "Server public IP address (from the Oracle instance page)" }
if (-not $Key) { $Key = Read-Host "Full path of the private key file you downloaded (e.g. C:\Users\you\Downloads\ssh-key-2026.key)" }
$Ip = $Ip.Trim(); $Key = $Key.Trim().Trim('"')
if (-not (Test-Path $Key)) { throw "Key file not found: $Key" }
Set-Content $SettingsFile @($Ip, $Key, $User)

# Windows ssh refuses keys other users could read - make it private to you
icacls $Key /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null
$Ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
$Target = "$User@$Ip"

Write-Host "== Checking the server answers ==" -ForegroundColor Cyan
& ssh @Ssh $Target "echo connected to `$(hostname)"
if ($LASTEXITCODE -ne 0) { throw "Can't reach the server. Check the IP, the key file, and that port 22 is open." }

Write-Host "== Packing the app ==" -ForegroundColor Cyan
$Stage = Join-Path $env:TEMP "stockapp-pack"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
$Dst = Join-Path $Stage "stock-app"
robocopy $App $Dst /E /NFL /NDL /NJH /NJS /NP /XD .venv data output __pycache__ /XF server.txt *.pyc .env RUNS_ON_SERVER | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Dst "data") | Out-Null
if (-not $CodeOnly) {
    Copy-Item (Join-Path $App ".env") $Dst
    # A consistent copy of the database even while the bot is writing to it
    & (Join-Path $App ".venv\Scripts\python.exe") -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" (Join-Path $App "data\market.db") (Join-Path $Dst "data\market.db")
}
$Tgz = Join-Path $env:TEMP "stock-app.tgz"
if (Test-Path $Tgz) { Remove-Item $Tgz -Force }
tar -czf $Tgz -C $Stage stock-app
Write-Host ("   {0:N0} MB" -f ((Get-Item $Tgz).Length / 1MB))

Write-Host "== Uploading (a few minutes the first time) ==" -ForegroundColor Cyan
& scp @Ssh $Tgz "${Target}:~/stock-app.tgz"
if ($LASTEXITCODE -ne 0) { throw "Upload failed." }

if ($CodeOnly) {
    Write-Host "== Updating the code on the server ==" -ForegroundColor Cyan
    & ssh @Ssh $Target "tar -xzf stock-app.tgz && rm stock-app.tgz && cd stock-app && .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart stockbot && sleep 5 && systemctl is-active stockbot"
} else {
    # Stop the PC bot first so only one copy ever answers Telegram
    Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -like "*main.py run*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "== Installing on the server (5-10 minutes the first time) ==" -ForegroundColor Cyan
    & ssh @Ssh $Target "tar -xzf stock-app.tgz && rm stock-app.tgz && cd stock-app && bash deploy/setup_server.sh"
}
if ($LASTEXITCODE -ne 0) { throw "Something failed on the server - see the messages above." }

# From now on the PC must not run the bot (menu 13 will refuse and explain)
Set-Content (Join-Path $App "data\RUNS_ON_SERVER") "Bot runs on $Ip since $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Remove-Item $Stage -Recurse -Force; Remove-Item $Tgz -Force

Write-Host ""
Write-Host "DONE - the bot now runs on the cloud server $Ip, day and night." -ForegroundColor Green
Write-Host "Send 'wallet' to your bot on Telegram to check. You can switch this PC off."
