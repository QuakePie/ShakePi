<#
.SYNOPSIS
    Download all ShakePie daily plots from the recorder to this computer.

.DESCRIPTION
    Copies every generated daily plot (dayplots/<UTC-day>/<YYYYMMDD>-<STATION>-CH<n>.png)
    from the Raspberry Pi into a local folder so the images can be organized and uploaded to a
    website. Run this file by double-clicking it, or from PowerShell. You will be asked for the
    Pi account password once per run.

.NOTES
    Pass -RemoteUser and -RemoteHost, or set SHAKEPIE_REMOTE_USER and SHAKEPIE_REMOTE_HOST.
#>

param(
    [string] $RemoteUser = $env:SHAKEPIE_REMOTE_USER,
    [string] $RemoteHost = $env:SHAKEPIE_REMOTE_HOST,
    [string] $RemoteDir = "/mnt/shakepie-data/dayplots",
    [string] $DestDir = (Join-Path $PSScriptRoot "dayplots")
)

$ErrorActionPreference = "Stop"

if (-not $RemoteUser) { $RemoteUser = Read-Host "Pi SSH username" }
if (-not $RemoteHost) { $RemoteHost = "raspberrypi.local" }
if ($RemoteUser -notmatch '^[A-Za-z0-9._-]+$') { throw "Invalid SSH username" }
if ($RemoteHost -notmatch '^[A-Za-z0-9._:-]+$') { throw "Invalid Pi hostname or IP address" }
if ($RemoteDir -notmatch '^/[A-Za-z0-9._/-]+$') { throw "RemoteDir must be a simple absolute path" }

function Main {
    if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
        Write-Host "scp was not found. Install the built-in Windows OpenSSH client:" -ForegroundColor Red
        Write-Host "  Settings > Apps > Optional features > Add 'OpenSSH Client'" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath $DestDir)) {
        New-Item -ItemType Directory -Path $DestDir | Out-Null
    }
    Write-Host "Downloading daily plots from $RemoteUser@$RemoteHost to $DestDir"
    Write-Host "Enter the password for $RemoteUser@$RemoteHost when prompted."
    Write-Host "Each file below shows its own progress bar (percent, size, speed)."
    & scp -r "$RemoteUser@${RemoteHost}:${RemoteDir}/*" "$DestDir"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The download failed (exit code $LASTEXITCODE). Check the address, the" -ForegroundColor Red
        Write-Host "network, and the password, then run this script again." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    $temporary = Join-Path $DestDir "tmp"
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -Recurse -Force -LiteralPath $temporary
    }
    $plots = Get-ChildItem -Path $DestDir -Recurse -File -Filter "*.png"
    Write-Host ("Done. {0} daily plot image(s) are available in {1}" -f $plots.Count, $DestDir) -ForegroundColor Green
    Write-Host "Each image is named YYYYMMDD-STATION-CHn.png (CH1/CH2/CH3 = N/E/Z), ready for upload."
}

Main
