# Installs a static ffmpeg on this server so the rotisserie/bench/RTSP camera reads work again.
# Delivered via the kitchen-deploy self-update; double-click INSTALL_FFMPEG.bat to run it.
$ErrorActionPreference = 'Stop'
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12   # 2012 R2 defaults to TLS1.0 -> downloads fail without this
} catch {}

$bin = 'C:\ffmpeg\bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$zip = Join-Path $env:TEMP 'ffmpeg_dl.zip'

Write-Host 'Downloading ffmpeg (static build)...  this can take a minute'
$urls = @(
  'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
  'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)
$got = $false
foreach ($u in $urls) {
  try { Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing; $got = $true; Write-Host ("Downloaded from " + $u); break }
  catch { Write-Host ("  (that source failed, trying the next one...)") }
}
if (-not $got) { Write-Host 'DOWNLOAD FAILED from every source. Tell Claude.'; Read-Host 'Press Enter to close'; exit 1 }

Write-Host 'Extracting...'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$tmp = Join-Path $env:TEMP 'ffmpeg_extract'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp)

$exe = Get-ChildItem -Path $tmp -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
if (-not $exe) { Write-Host 'Could not find ffmpeg.exe inside the download. Tell Claude.'; Read-Host 'Press Enter to close'; exit 1 }

Copy-Item $exe.FullName (Join-Path $bin 'ffmpeg.exe') -Force
# also drop it right next to the dashboard (this script's folder) so it is found even before a PATH refresh
try { Copy-Item $exe.FullName (Join-Path $PSScriptRoot 'ffmpeg.exe') -Force } catch {}

# add C:\ffmpeg\bin to the machine PATH if it isn't there yet
$p = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($p -notlike "*$bin*") {
  [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $bin), 'Machine')
  Write-Host 'Added C:\ffmpeg\bin to the system PATH.'
}

Write-Host ''
Write-Host '--- checking it runs on this Windows version ---'
try {
  $v = (& (Join-Path $bin 'ffmpeg.exe') -version 2>&1 | Select-Object -First 1)
  Write-Host $v
  Write-Host ''
  Write-Host 'FFMPEG INSTALLED OK.  Now restart the dashboard (double-click RESTART_DASHBOARD.bat) so it picks it up.'
} catch {
  Write-Host 'ffmpeg.exe would not run on this Windows version:'
  Write-Host $_.Exception.Message
  Write-Host 'Tell Claude - we will grab an older 2012-R2-compatible build.'
}
Read-Host 'Press Enter to close'
