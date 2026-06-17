# Bruno's Kitchen Dashboard - auto-deploy puller (SAFE, idempotent, PATH-hardened).
# A scheduled task runs this every few minutes. It pulls the latest code from GitHub and
# swaps it into the live folder when it differs - after validation + backup. It NEVER
# restarts the server: screen (dashboard_ui.html) changes are live on next page load;
# backend (dashboard_app.py) changes activate on the next restart/reboot.

$ErrorActionPreference = "SilentlyContinue"
$repo = "C:\KitchenDashboard-sync"
$app  = "C:\Users\me\Downloads\KitchenDashboard-ServerPC-2\KitchenDashboard-ServerPC"   # live dashboard folder (nested)
$log  = Join-Path $app "autodeploy.log"
function Log($m){ "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -FilePath $log }

# Scheduled tasks can run with a stripped-down PATH, so git/python may not be found by
# bare name (this is what made the auto-runs silently skip). Add the usual locations and
# resolve full paths with fallbacks.
$env:Path = "$env:Path;C:\Program Files\Git\cmd;C:\Program Files\Git\bin;C:\Windows;C:\Windows\System32;$env:LOCALAPPDATA\Programs\Python\Python313;$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python311"
$git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
if (-not $git) { foreach($f in @("C:\Program Files\Git\cmd\git.exe","C:\Program Files\Git\bin\git.exe")){ if(Test-Path $f){$git=$f;break} } }
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { foreach($f in @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe","$env:LOCALAPPDATA\Programs\Python\Python312\python.exe","$env:LOCALAPPDATA\Programs\Python\Python311\python.exe","C:\Python313\python.exe")){ if(Test-Path $f){$py=$f;break} } }

if (-not (Test-Path $repo)) { Log "sync folder missing - run setup-autodeploy.bat as admin"; exit }
if (-not $git) { Log "git not found (PATH) - cannot pull"; exit }
Set-Location $repo
& $git fetch origin main 2>$null
& $git reset --hard origin/main 2>$null     # match latest published version

$ui = Join-Path $repo "dashboard_ui.html"
$appfile = Join-Path $repo "dashboard_app.py"
$liveUi = Join-Path $app "dashboard_ui.html"
$livePy = Join-Path $app "dashboard_app.py"
$wb = Join-Path $repo "weekly-books.html"
$liveWb = Join-Path $app "weekly-books.html"
function Differs($src,$dst){ if (-not (Test-Path $dst)) { return $true }; return (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash }

# UI (screen) - validate basic integrity; deploy even if the backend check can't run
$uiOk = (Test-Path $ui) -and ((Get-Item $ui).Length -ge 50000) -and (Select-String -Path $ui -Pattern "Bruno" -Quiet)
$uiChanged = $uiOk -and (Differs $ui $liveUi)

# weekly-books (screen) - same as UI: live on next page load, no restart needed
$wbOk = (Test-Path $wb) -and ((Get-Item $wb).Length -ge 20000) -and (Select-String -Path $wb -Pattern "Bruno" -Quiet)
$wbChanged = $wbOk -and (Differs $wb $liveWb)

# backend (app.py) - only deploy if it compiles cleanly
$appOk = $false
if (Test-Path $appfile) {
  if ($py) { & $py -m py_compile $appfile 2>$null; $appOk = ($LASTEXITCODE -eq 0) }
  else { Log "python not found (PATH) - leaving backend unchanged this run" }
}
$appChanged = $appOk -and (Differs $appfile $livePy)

if (-not $uiChanged -and -not $appChanged -and -not $wbChanged) { exit }   # already up to date

$bk = Join-Path $app ("backup\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $bk | Out-Null
Copy-Item $liveUi $bk -ErrorAction SilentlyContinue
Copy-Item $livePy $bk -ErrorAction SilentlyContinue
Copy-Item $liveWb $bk -ErrorAction SilentlyContinue

if ($wbChanged) { Copy-Item $wb $liveWb -Force; Log "UPDATED weekly-books (live now)" }
if ($uiChanged) { Copy-Item $ui $liveUi -Force; Log "UPDATED screen (live now)" }
if ($appChanged) {
  Copy-Item $appfile $livePy -Force
  Log "UPDATED backend - RESTART or reboot to activate dashboard_app.py"
  "A backend update was downloaded $(Get-Date -Format 'yyyy-MM-dd HH:mm'). Restart the dashboard (or reboot) to activate it. Screen changes are already live." | Out-File -FilePath (Join-Path $app "BACKEND-UPDATE-PENDING.txt")
} elseif ($uiChanged) {
  Remove-Item (Join-Path $app "BACKEND-UPDATE-PENDING.txt") -ErrorAction SilentlyContinue
}
