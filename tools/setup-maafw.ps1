<#
.SYNOPSIS
    Install MaaFramework's Python binding (maafw) for simauto WITHOUT touching
    the system Python.

.DESCRIPTION
    A wheel is just a ZIP file, so this script downloads the wheels from PyPI,
    unpacks them into .\vendor\ in the project, and writes a setuptools-style
    .pth file into that directory.  simauto prepends .\vendor to sys.path, so
    `import maa` starts working with no pip, no admin rights and no changes
    outside this folder.

    Why not pip:  this machine's Python lives under a sandboxed runtime whose
    site-packages and %APPDATA%\Roaming are read-only, so pip cannot write
    anywhere it is allowed to.

    Wheels fetched:
      maafw         - the binding + MaaFramework.dll (the actual capture engine)
      strenum       - tiny pure-python dependency of the binding
      MaaAgentBinary- only needed for ADB-based control; fetched but optional

.PARAMETER Force
    Re-download and re-extract even if vendor\maa already exists.

.PARAMETER SkipAgentBinary
    Skip MaaAgentBinary (safe for Win32 window capture; it only ships the
    Android minitouch/maatouch helpers used by the ADB controller).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\setup-maafw.ps1
    powershell -ExecutionPolicy Bypass -File tools\setup-maafw.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipAgentBinary,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads much slower

$projectRoot = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $projectRoot "vendor"
$cache = Join-Path $projectRoot ".wheels"

function Write-Step($msg) { Write-Host "[..] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[x]  $msg" -ForegroundColor Red }

# --------------------------------------------------------------------------
# Pick the python used for the final check
# --------------------------------------------------------------------------
if (-not $Python) {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"),
        "python.exe"
    )
    foreach ($c in $candidates) {
        if ($c -eq "python.exe") {
            $cmd = Get-Command python -ErrorAction SilentlyContinue
            if ($cmd -and (Get-Item $cmd.Source).Length -gt 0) { $Python = $cmd.Source; break }
        } elseif (Test-Path $c) { $Python = $c; break }
    }
}

# --------------------------------------------------------------------------
# Already done?
# --------------------------------------------------------------------------
$maaDir = Join-Path $vendor "maa"
if ((Test-Path $maaDir) -and -not $Force) {
    Write-Ok "vendor\maa already exists. Use -Force to reinstall."
} else {

if (Test-Path $vendor) {
    Write-Step "clearing old vendor\"
    Remove-Item -Recurse -Force $vendor
}
New-Item -ItemType Directory -Force -Path $vendor, $cache | Out-Null

# --------------------------------------------------------------------------
# Resolve wheel URLs from the PyPI JSON API, filtered by this interpreter's
# Python version and platform tags.
# --------------------------------------------------------------------------
function Get-WheelUrl([string]$package, [string]$preferTag) {
    $meta = Invoke-RestMethod -Uri "https://pypi.org/pypi/$package/json" -TimeoutSec 60
    $files = $meta.urls
    # Prefer a wheel matching the tag, then any win_amd64 wheel, then pure python.
    $pick = $files | Where-Object { $_.filename -like "*$preferTag*" } | Select-Object -First 1
    if (-not $pick) { $pick = $files | Where-Object { $_.filename -like "*win_amd64.whl" } | Select-Object -First 1 }
    if (-not $pick) { $pick = $files | Where-Object { $_.filename -like "*py3-none-any.whl" } | Select-Object -First 1 }
    if (-not $pick) { throw "no suitable wheel found for $package" }
    return [pscustomobject]@{ Url = $pick.url; Name = $pick.filename; Version = $meta.info.version }
}

# Windows PowerShell 5.1 defaults to TLS 1.0, which PyPI rejects.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
} catch {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

$packages = @("maafw", "strenum")
if (-not $SkipAgentBinary) { $packages += "MaaAgentBinary" }

# The wheels are tagged py3-none-win_amd64 (the binding is ABI-agnostic), so no
# interpreter introspection is needed - only the architecture has to match.
$tag = "win_amd64"
Write-Step "target platform: $tag"
Write-Step "download cache:  $cache"

$downloaded = @()
foreach ($pkg in $packages) {
    try {
        $info = Get-WheelUrl $pkg $tag
        $dest = Join-Path $cache $info.Name
        if (Test-Path $dest) {
            Write-Ok "$($info.Name) (cached)"
        } else {
            Write-Step "downloading $($info.Name)  [$($info.Version)]"
            $sw = [Diagnostics.Stopwatch]::StartNew()
            (New-Object Net.WebClient).DownloadFile($info.Url, $dest)
            $sw.Stop()
            $mb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
            Write-Ok "$($info.Name)  ${mb} MB in $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
        }
        $downloaded += $dest
    } catch {
        if ($pkg -eq "MaaAgentBinary") {
            Write-Warn2 "MaaAgentBinary failed ($($_.Exception.Message)); continuing - only needed for ADB control"
        } else {
            Write-Err "failed to get $pkg : $($_.Exception.Message)"
            throw
        }
    }
}

# --------------------------------------------------------------------------
# Unpack.  A .whl is a ZIP: expand it and the package directories land in
# vendor\ directly.
# --------------------------------------------------------------------------
Write-Step "extracting into vendor\"
foreach ($whl in $downloaded) {
    $tmp = Join-Path $cache ("x_" + [IO.Path]::GetFileNameWithoutExtension($whl))
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    # Expand-Archive insists on a .zip extension.
    $asZip = "$whl.zip"
    Copy-Item $whl $asZip -Force
    Expand-Archive -Path $asZip -DestinationPath $tmp -Force
    Remove-Item $asZip -Force
    # Copy package payloads (skip dist-info metadata).
    Get-ChildItem $tmp | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.data" } | ForEach-Object {
        Copy-Item $_.FullName -Destination $vendor -Recurse -Force
    }
    Remove-Item -Recurse -Force $tmp
    Write-Ok "unpacked $(Split-Path -Leaf $whl)"
}

# Belt and braces: also copy anything inside *.data\purelib (some wheels put
# the payload there instead of at the archive root).
foreach ($whl in $downloaded) {
    $asZip = "$whl.zip"
    Copy-Item $whl $asZip -Force
    $tmp = Join-Path $cache ("d_" + [IO.Path]::GetFileNameWithoutExtension($whl))
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $asZip -DestinationPath $tmp -Force
    Remove-Item $asZip -Force
    $purelib = Get-ChildItem $tmp -Directory -Filter "*.data" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "purelib" } | Where-Object { Test-Path $_ }
    foreach ($pl in $purelib) {
        Get-ChildItem $pl | ForEach-Object {
            Copy-Item $_.FullName -Destination $vendor -Recurse -Force
        }
    }
    Remove-Item -Recurse -Force $tmp
}

# A .pth named anything works, but it must contain a bare path.
Set-Content -Path (Join-Path $vendor "simauto-vendor.pth") -Value $vendor -Encoding ASCII
}   # end of "not already installed"

# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
Write-Host ""
Write-Step "verifying"
$found = Get-ChildItem $vendor -Filter "MaaFramework.dll" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($found) {
    Write-Ok "MaaFramework.dll present: $($found.FullName.Substring($projectRoot.Length + 1)) ($([math]::Round($found.Length/1MB,1)) MB)"
} else {
    Write-Warn2 "MaaFramework.dll NOT found - maafw may have unpacked differently; check vendor\"
}

if (-not $Python) {
    Write-Warn2 "no usable python found for the import test; run `python -m simauto doctor` yourself"
    exit 0
}

Push-Location $projectRoot
try {
    $out = & $Python -c "import simauto; print('maa_available=', simauto.maa_available())" 2>&1
    Write-Host "     $out"
    if ("$out" -match "maa_available= True") {
        Write-Ok "MaaFramework backend is ready."
        Write-Host ""
        Write-Host "Next:  python -m simauto probe --title MuMu --backend maafw" -ForegroundColor White
    } else {
        Write-Warn2 "maafw still not importable; see the message above."
        Write-Host "     Try: python -m simauto doctor" -ForegroundColor White
    }
} finally {
    Pop-Location
}
