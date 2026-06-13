# Bruno's Kitchen Dashboard - auto-deploy puller (SAFE).
# A scheduled task runs this every few minutes on the shop server.
# It pulls the latest code from GitHub and swaps it into the live folder ONLY
# if it passes validation, after backing up the current files. It NEVER restarts
# the server: screen (dashboard_ui.html) changes are live on the next page load;
# backend (dashboard_app.py) changes activate on the next restart/reboot.

$ErrorActionPreference = "SilentlyContinue"
$repo = "C:\KitchenDashboard-sync"     # local clone of the GitHub repo
$app  = "C:\KitchenDashboard"          # the live dashboard folder
$log  = Join-Path $app "autodeploy.log"
function Log($m){ "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -FilePath $log }

if (-not (Test-Path $repo)) { Log "sync folder missing - run setup-autodeploy.bat"; exit }
Set-Location $repo

git fetch origin main 2>$null
$local  = (git rev-parse HEAD 2>$null).Trim()
$remote = (git rev-parse origin/main 2>$null).Trim()
if ($local -eq $remote -or [string]::IsNullOrEmpty($remote)) { exit }   # nothing new / offline
git reset --hard origin/main 2>$null

# ---- validate BEFORE touching the live folder ----
$ui = Join-Path $repo "dashboard_ui.html"
$py = Join-Path $repo "dashboard_app.py"
if (-not (Test-Path $ui) -or ((Get-Item $ui).Length -lt 50000) -or -not (Select-String -Path $ui -Pattern "Bruno" -Quiet)) { Log "UI failed validation - skipped"; exit }
python -m py_compile $py 2>$null
if ($LASTEXITCODE -ne 0) { Log "app.py failed to compile - skipped (live files untouched)"; exit }

# ---- back up current live files ----
$bk = Join-Path $app ("backup\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $bk | Out-Null
Copy-Item (Join-Path $app "dashboard_ui.html") $bk -ErrorAction SilentlyContinue
Copy-Item (Join-Path $app "dashboard_app.py")  $bk -ErrorAction SilentlyContinue

# ---- swap in ----
$liveApp = Join-Path $app "dashboard_app.py"
$appChanged = $true
if (Test-Path $liveApp) { $appChanged = (Get-FileHash $py).Hash -ne (Get-FileHash $liveApp).Hash }
Copy-Item $ui (Join-Path $app "dashboard_ui.html") -Force
Copy-Item $py $liveApp -Force

if ($appChanged) {
  Log "UPDATED incl. backend - RESTART or reboot to activate dashboard_app.py"
  "A backend (dashboard_app.py) update was downloaded $(Get-Date -Format 'yyyy-MM-dd HH:mm'). Restart the dashboard window (or reboot the PC) to activate it. Screen changes are already live." | Out-File -FilePath (Join-Path $app "BACKEND-UPDATE-PENDING.txt")
} else {
  Log "UPDATED screen only - already live"
  Remove-Item (Join-Path $app "BACKEND-UPDATE-PENDING.txt") -ErrorAction SilentlyContinue
}
