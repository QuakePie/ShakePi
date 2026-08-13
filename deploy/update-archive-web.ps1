<#
.SYNOPSIS
    Ship the newest archive-web wheel to the recorder and restart its services.

.DESCRIPTION
    Copies the newest archive-web/dist/shakepie_archive-*.whl to the Pi, installs it into
    /opt/shakepie/archive-web/.venv, and restarts the web and dayplot-worker services.
    After rebuilding the wheel (from the archive-web folder:
    ".venv\Scripts\python -m pip wheel . -w dist --no-deps"), run this file by
    double-clicking it or from PowerShell. You will be asked for the Pi account password
    for the upload and again for sudo inside the SSH session.

.NOTES
    Pass -RemoteUser and -RemoteHost, or set SHAKEPIE_REMOTE_USER and SHAKEPIE_REMOTE_HOST.
#>

param(
    [string] $RemoteUser = $env:SHAKEPIE_REMOTE_USER,
    [string] $RemoteHost = $env:SHAKEPIE_REMOTE_HOST,
    [string] $RemoteVenvPip = "/opt/shakepie/archive-web/.venv/bin/pip"
)

$ErrorActionPreference = "Stop"

if (-not $RemoteUser) { $RemoteUser = Read-Host "Pi SSH username" }
if (-not $RemoteHost) { $RemoteHost = "raspberrypi.local" }
if ($RemoteUser -notmatch '^[A-Za-z0-9._-]+$') { throw "Invalid SSH username" }
if ($RemoteHost -notmatch '^[A-Za-z0-9._:-]+$') { throw "Invalid Pi hostname or IP address" }
if ($RemoteVenvPip -notmatch '^/[A-Za-z0-9._/-]+$') { throw "RemoteVenvPip must be a simple absolute path" }
$Services = @("shakepie-archive-web.service", "shakepie-dayplot-worker.service")

function Main {
    $dist = Join-Path $PSScriptRoot "..\archive-web\dist"
    $wheel = Get-ChildItem -Path $dist -Filter "shakepie_archive-*.whl" -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $wheel) {
        Write-Host "No wheel found in $dist" -ForegroundColor Red
        Write-Host "Build one first, from the archive-web folder:"
        Write-Host "  .venv\Scripts\python -m pip wheel . -w dist --no-deps"
        exit 1
    }
    Write-Host "Shipping $($wheel.Name) to $RemoteUser@$RemoteHost"
    Write-Host "Enter the password for $RemoteUser@$RemoteHost when prompted."
    & scp -q $wheel.FullName "$RemoteUser@${RemoteHost}:/tmp/"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The upload failed (exit code $LASTEXITCODE). Check the address, the" -ForegroundColor Red
        Write-Host "network, and the password, then run this script again." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    $remote = "sudo $RemoteVenvPip install --force-reinstall /tmp/$($wheel.Name) && " +
        "sudo systemctl restart $($Services -join ' ') && rm /tmp/$($wheel.Name)"
    Write-Host "Installing and restarting $($Services -join ', ')."
    Write-Host "Enter your sudo password on the Pi if it is requested."
    & ssh -t "$RemoteUser@$RemoteHost" $remote
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The remote install failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "Done. Open the Dayplot page and hard-refresh it (Ctrl+F5)." -ForegroundColor Green
    Write-Host "'Regenerate everything' with the 'Regenerate all days' button sits below the generate panel."
}

Main
