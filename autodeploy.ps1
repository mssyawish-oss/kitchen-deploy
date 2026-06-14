# Bruno's Kitchen Dashboard - auto-deploy puller (SAFE, idempotent).
# A scheduled task runs this every few minutes on the shop server.
# It pulls the latest code from GitHub and swaps it into the live folder whenever the
# synced files differ from the live ones - ONLY if they pass validation, after backing
# up the current files. It NEVER restarts the server: screen (dashboard_ui.html) changes
# are live on the next page load; backend (dashboard_app.py) changes activate on the
# next restart/reboot.

$ErrorActionPreference = "SilentlyContinue"
$repo = "C:\KitchenDashboard-sync"     # local clone of the GitHub repo
$app  = "C:\KitchenDashboard"          # the live dashboard folder
$log  = Join-Path $app "autodeploy.log"
function Log($m){ "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -FilePath $log }

if (-not (Test-Path $repo)) { Log "sync folder missing - run setup-autodeploy.bat as admin"; exit }
Set-Location $repo
git fetch origin main 2>$null
git reset --hard origin/main 2>$null   # always match the latest published version (no-op if already current)

# ---- validate BEFORE touching the live folder ----
$ui = Join-Path $repo "dashboard_ui.html"
$py = Join-Path $repo "dashboard_app.py"
if (-not (Test-Path $ui) -or ((Get-Item $ui).Length -lt 50000) -or -not (Select-String -Path $ui -Pattern "Bruno" -Quiet)) { Log "UI failed validation - skipped"; exit }
python -m py_compile $py 2>$null
if ($LASTEXITCODE -ne 0) { Log "app.py failed to compile - skipped (live files untouched)"; exit }

# ---- deploy only the files that actually differ from the live copy ----
$liveUi = Join-Path $app "dashboard_ui.html"
$livePy = Join-Path $app "dashboard_app.py"
function Differs($src,$dst){ if (-not (Test-Path $dst)) { return $true }; return (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash }
$uiChanged  = Differs $ui $liveUi
$appChanged = Differs $py $livePy
if (-not $uiChanged -and -not $appChanged) { exit }   # already up to date - nothing to do

# ---- back up current live files ----
$bk = Join-Path $app ("backup\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $bk | Out-Null
Copy-Item $liveUi $bk -ErrorAction SilentlyContinue
Copy-Item $livePy $bk -ErrorAction SilentlyContinue

if ($uiChanged)  { Copy-Item $ui $liveUi -Force }
if ($appChanged) {
  Copy-Item $py $livePy -Force
  Log "UPDATED incl. backend - RESTART or reboot to activate dashboard_app.py"
  "A backend (dashboard_app.py) update was downloaded $(Get-Date -Format 'yyyy-MM-dd HH:mm'). Restart the dashboard window (or reboot the PC) to activate it. Screen changes are already live." | Out-File -FilePath (Join-Path $app "BACKEND-UPDATE-PENDING.txt")
} else {
  Log "UPDATED screen only - already live"
  Remove-Item (Join-Path $app "BACKEND-UPDATE-PENDING.txt") -ErrorAction SilentlyContinue
}
