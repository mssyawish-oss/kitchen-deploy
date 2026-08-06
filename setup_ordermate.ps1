# Bruno's Kitchen Dashboard - ORDERMATE test install
# Run in an ADMIN PowerShell on ORDERMATE. Installs Python 3.12 + deps + ffmpeg, fetches the app
# from the public repo, and starts a TEST instance with an EMPTY data file.
#
# SAFE BY DESIGN: the test instance has no Square token, no ThermoWorks login and no printer config,
# so it cannot touch the live shop. Self-update is disabled. Nothing on the Surface is altered.

$ErrorActionPreference = "Continue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12   # 2012 R2 defaults to TLS 1.0; GitHub/python.org refuse it
try{ Start-Transcript -Path "$env:TEMP\ordermate-transcript.log" -Force | Out-Null }catch{}   # the .bat copies this onto the USB

$TEST = "C:\KitchenDash-Test"
function Say($m){ Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m){ Write-Host "  OK   $m" -ForegroundColor Green }
function Bad($m){ Write-Host "  FAIL $m" -ForegroundColor Red }

if(-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
   ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
  Bad "Not running as Administrator. Right-click PowerShell and Run as administrator, then paste again."
  return
}

Say "0. This machine"
Write-Host ("  " + (Get-CimInstance Win32_OperatingSystem).Caption)
Write-Host ("  PowerShell " + $PSVersionTable.PSVersion)

Say "1. Universal C Runtime (Python 3.9+ will not run without it)"
$ucrt = Test-Path "$env:SystemRoot\System32\ucrtbase.dll"
if($ucrt){ Ok "ucrtbase.dll present" }
else{ Bad "ucrtbase.dll MISSING - install Windows Update KB2999226 first, then re-run this script." }

Say "2. Python 3.12 (last version that supports Server 2012 R2)"
$py = $null
try{ $py = (Get-Command python -ErrorAction Stop).Source }catch{}
$needPy = $true
if($py){
  $v = (& python -V 2>&1) -join ""
  if($v -match "3\.12"){ Ok "$v already installed"; $needPy = $false } else { Write-Host "  found $v - installing 3.12 alongside" }
}
if($needPy){
  $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
  $exe = "$env:TEMP\python-3.12.10-amd64.exe"
  Write-Host "  downloading $url"
  try{
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    Write-Host "  installing (silent, this takes a minute)..."
    Start-Process -FilePath $exe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_test=0" -Wait
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    $v = (& python -V 2>&1) -join ""
    if($v -match "3\.12"){ Ok $v } else { Bad "python did not report 3.12 after install (got '$v')" }
  }catch{ Bad ("python download/install failed: " + $_.Exception.Message) }
}

Say "3. Python packages"
& python -m pip install --upgrade pip --quiet 2>&1 | Out-Null
foreach($p in @("flask","certifi","pypdf","pillow","soco","thermoworks-cloud","aiohttp")){
  & python -m pip install $p --quiet 2>&1 | Out-Null
  $c = & python -c "import importlib.util,sys; n='$p'.replace('-','_'); n={'pillow':'PIL','thermoworks_cloud':'thermoworks_cloud'}.get(n,n); sys.stdout.write('1' if importlib.util.find_spec(n) else '0')" 2>&1
  if("$c" -eq "1"){ Ok $p } else { Bad "$p did not import" }
}
Write-Host "  bleak (Bluetooth) is EXPECTED to fail here - labels go via the relay instead"
& python -m pip install bleak --quiet 2>&1 | Out-Null

Say "4. ffmpeg (camera + rotisserie counting)"
if(Get-Command ffmpeg -ErrorAction SilentlyContinue){ Ok "already on PATH" }
else{
  try{
    $z = "$env:TEMP\ffmpeg.zip"
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $z -UseBasicParsing
    if(Test-Path "C:\ffmpeg"){ Remove-Item "C:\ffmpeg" -Recurse -Force -ErrorAction SilentlyContinue }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($z,"C:\ffmpeg-tmp")
    $bin = (Get-ChildItem "C:\ffmpeg-tmp" -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
    New-Item -ItemType Directory -Path "C:\ffmpeg" -Force | Out-Null
    Copy-Item "$bin\*" "C:\ffmpeg" -Force
    Remove-Item "C:\ffmpeg-tmp" -Recurse -Force -ErrorAction SilentlyContinue
    $m = [Environment]::GetEnvironmentVariable("Path","Machine")
    if($m -notlike "*C:\ffmpeg*"){ [Environment]::SetEnvironmentVariable("Path","$m;C:\ffmpeg","Machine") }
    $env:Path += ";C:\ffmpeg"
    if(Get-Command ffmpeg -ErrorAction SilentlyContinue){ Ok "installed to C:\ffmpeg" } else { Bad "ffmpeg still not on PATH" }
  }catch{ Bad ("ffmpeg failed: " + $_.Exception.Message) }
}

Say "5. Fetch the dashboard (public repo)"
try{
  $z = "$env:TEMP\kd.zip"
  Invoke-WebRequest -Uri "https://github.com/mssyawish-oss/kitchen-deploy/archive/refs/heads/main.zip" -OutFile $z -UseBasicParsing
  if(Test-Path $TEST){ Remove-Item $TEST -Recurse -Force -ErrorAction SilentlyContinue }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::ExtractToDirectory($z,"$env:TEMP\kd")
  $src = (Get-ChildItem "$env:TEMP\kd" -Directory | Select-Object -First 1).FullName
  New-Item -ItemType Directory -Path $TEST -Force | Out-Null
  Copy-Item "$src\*" $TEST -Recurse -Force
  Remove-Item "$env:TEMP\kd" -Recurse -Force -ErrorAction SilentlyContinue
  Ok "app in $TEST"
}catch{ Bad ("repo download failed: " + $_.Exception.Message) }

Say "6. Empty data file (NO credentials - cannot reach the live shop)"
"{}" | Set-Content "$TEST\kitchen_data.json" -Encoding ASCII
Ok "wrote an empty kitchen_data.json"

Say "7. Firewall - allow 8080 on the private network"
netsh advfirewall firewall delete rule name="Kitchen Dashboard 8080" 2>&1 | Out-Null
netsh advfirewall firewall add rule name="Kitchen Dashboard 8080" dir=in action=allow protocol=TCP localport=8080 profile=private,domain 2>&1 | Out-Null
Ok "rule added"

Say "8. Start the test dashboard"
$env:DASH_NO_SELFUPDATE = "1"    # must NOT self-update or restart itself during a test
# Start-Process inherits this PowerShell's environment, so DASH_NO_SELFUPDATE carries over.
# (-Environment does not exist on PowerShell 4/5 - do not add it.)
Start-Process -FilePath "python" -ArgumentList "dashboard_app.py" -WorkingDirectory $TEST -WindowStyle Minimized
Start-Sleep -Seconds 12
try{
  $r = Invoke-WebRequest -Uri "http://localhost:8080/api/selfupdate_status" -UseBasicParsing -TimeoutSec 20
  Ok ("dashboard responded: HTTP " + $r.StatusCode)
}catch{
  Bad ("no response on 8080 yet: " + $_.Exception.Message)
  Write-Host "  If it did not start, run this by hand to see the error:" -ForegroundColor Yellow
  Write-Host "     cd $TEST ; python dashboard_app.py" -ForegroundColor Yellow
}

Say "DONE"
Write-Host "  Test dashboard should be at  http://192.168.0.33:8080" -ForegroundColor Cyan
Write-Host "  Tell Claude it's up and it will verify the rest over the network."
Write-Host "  This changed NOTHING on the Surface and holds no credentials."
try{ Stop-Transcript | Out-Null }catch{}
