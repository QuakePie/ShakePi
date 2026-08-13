[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $HostName,
    [Parameter(Mandatory)] [string] $UserName,
    [string] $SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $RemoteRoot = "/opt/shakepie",
    [string] $ReleaseId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"),
    [string] $ServiceName = "seisberry-recorder.service"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($HostName -notmatch '^[A-Za-z0-9._:-]+$') { throw "HostName contains unsupported characters" }
if ($UserName -notmatch '^[A-Za-z0-9._-]+$') { throw "UserName contains unsupported characters" }
if ($RemoteRoot -notmatch '^/[A-Za-z0-9._/-]+$') { throw "RemoteRoot must be a simple absolute path" }
if ($ReleaseId -notmatch '^[A-Za-z0-9._-]+$') { throw "ReleaseId contains unsupported characters" }
if ($ServiceName -notmatch '^[A-Za-z0-9@._-]+$') { throw "ServiceName contains unsupported characters" }

$resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path
$target = "$UserName@$HostName"
$staging = "/tmp/shakepie-$ReleaseId"
$release = "$RemoteRoot/releases/$ReleaseId"
$archive = Join-Path ([IO.Path]::GetTempPath()) "shakepie-$ReleaseId.tar.gz"
$previous = ""
$serviceStopped = $false

function Invoke-Ssh {
    param([Parameter(Mandatory)] [string] $Command)
    & ssh -o BatchMode=yes -o ConnectTimeout=10 -- $target $Command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed with exit code $LASTEXITCODE" }
}

try {
    Write-Host "1/9 Verifying SSH connectivity"
    Invoke-Ssh -Command "true"

    Write-Host "2/9 Packaging and copying source"
    # Package only project sources so unrelated local files cannot enter the archive.
    $sourceEntries = @(
        ".clang-tidy", ".gitattributes", ".github", ".gitignore",
        "CMakeLists.txt", "LICENSE", "README.md",
        "archive-web", "deploy", "docs", "laptop", "oled", "pi", "protocol", "terminal", "tests"
    )
    & tar -czf $archive --exclude=.git --exclude=.aws --exclude=backup --exclude=3D-Print --exclude=build --exclude=build-tidy --exclude=.venv --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude=.pytest-tmp --exclude=.mypy_cache --exclude=.ruff_cache --exclude=dayplots --exclude=recordings --exclude=ShakePieOffload --exclude='*.partial' --exclude='*.sqlite*' --exclude='*.env' --exclude=.env --exclude='.env.*' --exclude='*.pem' --exclude='*.key' -C $resolvedSource @sourceEntries
    if ($LASTEXITCODE -ne 0) { throw "Source archive creation failed" }
    Invoke-Ssh -Command "rm -rf -- '$staging' && mkdir -p '$staging'"
    & scp -q -- $archive "${target}:$staging/source.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "Source upload failed" }
    Invoke-Ssh -Command "tar -xzf '$staging/source.tar.gz' -C '$staging'"

    Write-Host "3/9 Building and running hardware-independent tests on the Pi"
    Invoke-Ssh -Command "cmake -S '$staging' -B '$staging/build' -DCMAKE_BUILD_TYPE=Release -DSHAKEPIE_BUILD_PI=ON -DSHAKEPIE_BUILD_TESTS=ON && cmake --build '$staging/build' --parallel && ctest --test-dir '$staging/build' --output-on-failure"

    $previous = (& ssh -o BatchMode=yes -- $target "readlink -f '$RemoteRoot/current' 2>/dev/null || true").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not read current release" }

    Write-Host "4/9 Stopping service"
    Invoke-Ssh -Command "sudo systemctl stop '$ServiceName' || true"
    $serviceStopped = $true

    Write-Host "5/9 Installing versioned release"
    Invoke-Ssh -Command "set -eu; getent group shakepie >/dev/null || sudo groupadd --system shakepie; getent group spi >/dev/null || sudo groupadd --system spi; getent group gpio >/dev/null || sudo groupadd --system gpio; id -u shakepie >/dev/null 2>&1 || sudo useradd --system --gid shakepie --groups spi,gpio --home-dir /nonexistent --shell /usr/sbin/nologin shakepie; sudo usermod -a -G spi,gpio shakepie; if sudo test -e '$release'; then echo 'release already exists' >&2; exit 1; fi; sudo mkdir -p '$release'; sudo cmake --install '$staging/build' --prefix '$release'; sudo mkdir -p '$release/share/doc/shakepie'; sudo cp -a '$staging/docs/.' '$release/share/doc/shakepie/'; sudo install -D -m 0644 '$staging/deploy/seisberry-recorder.service' '/etc/systemd/system/$ServiceName'; sudo install -D -m 0644 '$staging/deploy/99-shakepie.rules' '/etc/udev/rules.d/99-shakepie.rules'; sudo install -d -o root -g shakepie -m 0750 /etc/shakepie; if [ ! -f /etc/shakepie/recorder.conf ]; then sudo install -o root -g shakepie -m 0640 '$staging/pi/config/recorder.conf' /etc/shakepie/recorder.conf; else sudo chown root:shakepie /etc/shakepie/recorder.conf; sudo chmod 0640 /etc/shakepie/recorder.conf; fi"

    Write-Host "6/9 Updating atomic current symlink"
    Invoke-Ssh -Command "sudo ln -sfn '$release' '$RemoteRoot/current.next' && sudo mv -Tf '$RemoteRoot/current.next' '$RemoteRoot/current'"

    Write-Host "7/9 Reloading rules and starting service"
    Invoke-Ssh -Command "sudo udevadm control --reload-rules && sudo systemctl daemon-reload && sudo systemctl enable '$ServiceName' && sudo systemctl start '$ServiceName'"

    Write-Host "8/9 Verifying health"
    Invoke-Ssh -Command "for attempt in 1 2 3 4 5 6 7 8 9 10; do if sudo '$RemoteRoot/current/bin/shakepie-health' >/tmp/shakepie-health.out 2>/dev/null && systemctl is-active --quiet '$ServiceName'; then cat /tmp/shakepie-health.out; exit 0; fi; sleep 1; done; exit 1"

    Write-Host "9/9 Recent service logs"
    Invoke-Ssh -Command "sudo journalctl -u '$ServiceName' -n 50 --no-pager"
    $serviceStopped = $false
    Write-Host "Deployment succeeded: $release"
}
catch {
    $deploymentError = $_
    if ($serviceStopped -and $previous) {
        Write-Warning "Deployment failed after service stop; restoring $previous"
        try {
            Invoke-Ssh -Command "sudo systemctl stop '$ServiceName' || true; sudo ln -sfn '$previous' '$RemoteRoot/current.next' && sudo mv -Tf '$RemoteRoot/current.next' '$RemoteRoot/current' && sudo systemctl daemon-reload && sudo systemctl start '$ServiceName'"
            $serviceStopped = $false
        }
        catch {
            Write-Warning "Automatic rollback failed: $_"
        }
    }
    throw $deploymentError
}
finally {
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    try { Invoke-Ssh -Command "rm -rf -- '$staging'" } catch { Write-Warning "Could not remove remote staging directory: $_" }
    if ($serviceStopped -and $previous) {
        Write-Warning "Service may be stopped; the previous release was $previous"
    }
}
