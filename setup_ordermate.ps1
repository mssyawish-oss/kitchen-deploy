# Bruno's Kitchen Dashboard - ORDERMATE test install
#
# RUNS WITHOUT ADMINISTRATOR RIGHTS. The server has no keyboard, so a UAC prompt asking for an
# admin username/password cannot be answered - everything here installs per-user instead:
#   Python   -> per-user install (InstallAllUsers=0)
#   ffmpeg   -> %LOCALAPPDATA%\ffmpeg, added to the USER PATH
#   app      -> %USERPROFILE%\KitchenDash-Test
#   firewall -> attempted, skipped without complaint if not permitted
#
# SAFE BY DESIGN: the test instance gets an EMPTY settings file, so it has no Square token, no
# ThermoWorks login and no printer config. It cannot touch the live shop. Self-update is off.
# Nothing on the Surface (the current live server) is altered.

$ErrorActionPreference = "Continue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12   # 2012 R2 defaults to TLS 1.0; GitHub/python.org refuse it
try{ Start-Transcript -Path "$env:TEMP\ordermate-transcript.log" -Force | Out-Null }catch{}

$TEST = "$env:USERPROFILE\KitchenDash-Test"
$FF   = "$env:LOCALAPPDATA\ffmpeg"
function Say($m){ Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m){  Write-Host "  OK   $m" -ForegroundColor Green }
function Bad($m){ Write-Host "  FAIL $m" -ForegroundColor Red }

Say "0. This machine"
try{
  Write-Host ("  " + (Get-WmiObject Win32_OperatingSystem).Caption)
  Write-Host ("  user: $env:USERNAME    PowerShell " + $PSVersionTable.PSVersion)
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  Write-Host ("  administrator: " + $admin + "   (not required)")
}catch{ Write-Host "  (could not read machine info)" }

Say "1. Universal C Runtime (Python 3.9+ needs it)"
if(Test-Path "$env:SystemRoot\System32\ucrtbase.dll"){ Ok "ucrtbase.dll present" }
else{ Bad "ucrtbase.dll MISSING - needs Windows Update KB2999226. Python will not run without it." }

Say "2. Python 3.12 (last version supporting Server 2012 R2)"
$PY = $null
foreach($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe","python")){
  try{
    $v = (& $c -V 2>&1) -join ""
    if($v -match "3\.12"){ $PY = $c; Ok "$v  ($c)"; break }
  }catch{}
}
if(-not $PY){
  $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
  $exe = "$env:TEMP\python-3.12.10-amd64.exe"
  Write-Host "  downloading Python 3.12.10 ..."
  try{
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    Write-Host "  installing per-user (no admin needed, takes a minute) ..."
    Start-Process -FilePath $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0" -Wait
    $cand = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if(Test-Path $cand){ $PY = $cand; Ok ((& $PY -V 2>&1) -join "") }
    else{ Bad "Python installed but python.exe not found at $cand" }
  }catch{ Bad ("Python download/install failed: " + $_.Exception.Message) }
}
if(-not $PY){ Bad "Cannot continue without Python."; try{Stop-Transcript|Out-Null}catch{}; return }

Say "3. Python packages"
& $PY -m pip install --upgrade pip --quiet 2>&1 | Out-Null
$mods = @{ "flask"="flask"; "certifi"="certifi"; "pypdf"="pypdf"; "pillow"="PIL";
           "soco"="soco"; "thermoworks-cloud"="thermoworks_cloud"; "aiohttp"="aiohttp" }
foreach($p in $mods.Keys){
  & $PY -m pip install $p --quiet 2>&1 | Out-Null
  $m = $mods[$p]
  $r = & $PY -c "import importlib.util,sys; sys.stdout.write('1' if importlib.util.find_spec('$m') else '0')" 2>&1
  if("$r" -eq "1"){ Ok $p } else { Bad "$p did not import" }
}
Write-Host "  bleak (Bluetooth) is EXPECTED to fail on Windows Server - labels use the relay instead" -ForegroundColor Yellow
& $PY -m pip install bleak --quiet 2>&1 | Out-Null

Say "4. ffmpeg (camera + rotisserie counting)"
$ffexe = "$FF\ffmpeg.exe"
if(Test-Path $ffexe){ Ok "already at $FF" }
else{
  try{
    $z = "$env:TEMP\ffmpeg.zip"
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $z -UseBasicParsing
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if(Test-Path "$env:TEMP\ffx"){ Remove-Item "$env:TEMP\ffx" -Recurse -Force -ErrorAction SilentlyContinue }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($z,"$env:TEMP\ffx")
    $bin = (Get-ChildItem "$env:TEMP\ffx" -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
    New-Item -ItemType Directory -Path $FF -Force | Out-Null
    Copy-Item "$bin\*" $FF -Force
    Remove-Item "$env:TEMP\ffx" -Recurse -Force -ErrorAction SilentlyContinue
    $u = [Environment]::GetEnvironmentVariable("Path","User")
    if($u -notlike "*$FF*"){ [Environment]::SetEnvironmentVariable("Path","$u;$FF","User") }   # USER path - no admin
    $env:Path += ";$FF"
    if(Test-Path $ffexe){ Ok "installed to $FF" } else { Bad "ffmpeg.exe not found after extract" }
  }catch{ Bad ("ffmpeg failed: " + $_.Exception.Message) }
}

Say "5. Fetch the dashboard (your public repo)"
try{
  $z = "$env:TEMP\kd.zip"
  Invoke-WebRequest -Uri "https://github.com/mssyawish-oss/kitchen-deploy/archive/refs/heads/main.zip" -OutFile $z -UseBasicParsing
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  if(Test-Path "$env:TEMP\kd"){ Remove-Item "$env:TEMP\kd" -Recurse -Force -ErrorAction SilentlyContinue }
  [System.IO.Compression.ZipFile]::ExtractToDirectory($z,"$env:TEMP\kd")
  $src = (Get-ChildItem "$env:TEMP\kd" -Directory | Select-Object -First 1).FullName
  New-Item -ItemType Directory -Path $TEST -Force | Out-Null
  Copy-Item "$src\*" $TEST -Recurse -Force
  Remove-Item "$env:TEMP\kd" -Recurse -Force -ErrorAction SilentlyContinue
  Ok "app in $TEST"
}catch{ Bad ("repo download failed: " + $_.Exception.Message) }

Say "6. Empty settings file (no credentials - cannot reach the live shop)"
"{}" | Set-Content "$TEST\kitchen_data.json" -Encoding ASCII
Ok "wrote an empty kitchen_data.json"

Say "7. Firewall (needs admin - skipped quietly if not allowed)"
try{
  netsh advfirewall firewall delete rule name="Kitchen Dashboard 8080" 2>&1 | Out-Null
  $r = netsh advfirewall firewall add rule name="Kitchen Dashboard 8080" dir=in action=allow protocol=TCP localport=8080 profile=private,domain 2>&1
  if("$r" -match "Ok"){ Ok "port 8080 opened" } else { Write-Host "  (skipped - no admin rights; local test still works)" -ForegroundColor Yellow }
}catch{ Write-Host "  (skipped - no admin rights)" -ForegroundColor Yellow }

Say "8. Start the test dashboard"
$env:DASH_NO_SELFUPDATE = "1"
Start-Process -FilePath $PY -ArgumentList "dashboard_app.py" -WorkingDirectory $TEST -WindowStyle Minimized
Start-Sleep -Seconds 15
$up = $false
try{
  $r = Invoke-WebRequest -Uri "http://localhost:8080/api/selfupdate_status" -UseBasicParsing -TimeoutSec 25
  Ok ("dashboard answered on localhost: HTTP " + $r.StatusCode)
  $up = $true
}catch{ Bad ("no answer on 8080: " + $_.Exception.Message) }

if(-not $up){
  Say "9. Capturing the actual error"
  try{
    $out = & $PY "$TEST\dashboard_app.py" 2>&1 | Select-Object -First 40
    Write-Host ($out -join "`n") -ForegroundColor Yellow
  }catch{ Write-Host ("could not run it directly: " + $_.Exception.Message) -ForegroundColor Yellow }
}

Say "RESULT"
if($up){
  Write-Host "  WORKING - the dashboard runs on this server." -ForegroundColor Green
  Write-Host "  http://192.168.0.33:8080  (needs the firewall rule above to be reachable from other machines)"
}else{
  Write-Host "  NOT WORKING YET - the error is printed above and saved in the log." -ForegroundColor Red
}
Write-Host "  Nothing on the Surface was changed. No credentials are stored here."
try{ Stop-Transcript | Out-Null }catch{}
