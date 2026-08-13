# Deployment and transfer tools

Use your own Pi SSH username and hostname. These scripts never store passwords; OpenSSH
uses your SSH key or prompts for a password. The SSH console in
[`terminal/`](../terminal/README.md) offers batch transfers with skip-existing and optional
SHA-256 verification.

## Download dayplots

Linux/macOS:

```sh
SHAKEPIE_REMOTE_USER=your-user SHAKEPIE_REMOTE_HOST=raspberrypi.local \
  sh deploy/download-dayplots.sh
```

Optional environment variables: `SHAKEPIE_REMOTE_DIR` (default
`/mnt/shakepie-data/dayplots`) and `SHAKEPIE_DAYPLOT_DEST` (default `deploy/dayplots`).

Windows PowerShell:

```powershell
.\deploy\download-dayplots.ps1 -RemoteUser your-user -RemoteHost raspberrypi.local
```

Use `-RemoteDir` or `-DestDir` to override the source or destination. Both Windows scripts
also accept `SHAKEPIE_REMOTE_USER` and `SHAKEPIE_REMOTE_HOST` environment variables.
Double-click `download-dayplots.bat` to use the same script and enter the username when prompted.
These scripts copy the remote plot tree and may overwrite existing local plots.
Downloaded files stay local and are ignored by Git.

## Update the archive web service

Build a wheel from `archive-web/` using its own environment:

```powershell
.\.venv\Scripts\python.exe -m pip wheel . -w dist --no-deps
```

Then, from the repository root:

```powershell
.\deploy\update-archive-web.ps1 -RemoteUser your-user -RemoteHost raspberrypi.local
```

This uploads the newest wheel, installs it in `/opt/shakepie/archive-web/.venv`, and restarts
the web and dayplot-worker services. Override the installer path with `-RemoteVenvPip`.
The `.bat` wrapper accepts the same arguments or prompts for the username when double-clicked.

## Install the recorder

Follow [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) for first-time setup. With SSH keys and
the documented sudo permissions already configured:

```powershell
.\deploy\Deploy-ShakePie.ps1 -HostName raspberrypi.local -UserName your-user
```

The installer builds and tests on the Pi, installs a versioned release, updates the current
symlink, and restarts the recorder. It attempts rollback if deployment fails after stopping
the service. Hardware validation remains separate.
