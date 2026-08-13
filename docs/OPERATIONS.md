# Installation and operation

## One-time Pi setup

Create service groups/account without granting root execution:

```text
sudo groupadd --system shakepie
getent group spi >/dev/null || sudo groupadd --system spi
getent group gpio >/dev/null || sudo groupadd --system gpio
sudo useradd --system --gid shakepie --groups spi,gpio \
  --home-dir /nonexistent --shell /usr/sbin/nologin shakepie
```

Mount the HDD at `/mnt/shakepie-data` using `/etc/fstab` (prefer a UUID), verify it with `findmnt --mountpoint /mnt/shakepie-data`, then create the data directory:

```text
sudo install -d -o shakepie -g shakepie -m 0750 /mnt/shakepie-data/recordings
sudo install -d -o root -g shakepie -m 0750 /etc/shakepie
sudo install -m 0640 pi/config/recorder.conf /etc/shakepie/recorder.conf
sudo install -m 0644 deploy/99-shakepie.rules /etc/udev/rules.d/99-shakepie.rules
sudo install -m 0644 deploy/seisberry-recorder.service /etc/systemd/system/seisberry-recorder.service
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable --now seisberry-recorder.service
```

If the HDD mount, SPI device, or gpiochip differs, update both `recorder.conf` and the service's `RequiresMountsFor`, `ConditionPathIsMountPoint`, `ReadWritePaths`, and `DeviceAllow` values. Run `systemctl edit seisberry-recorder.service` so package updates do not overwrite local changes.

Inspect health and logs:

```text
sudo /opt/shakepie/current/bin/shakepie-health
sudo systemctl status seisberry-recorder.service
sudo journalctl -u seisberry-recorder.service -n 100 --no-pager
```

Graceful stop uses SIGTERM and drains the disk queue before footer, sync, close, rename, and directory sync:

```text
sudo systemctl stop seisberry-recorder.service
```

## Live connection

Keep the recorder bound to loopback. From Windows or Linux on the laptop:

```text
ssh -N -L 8765:127.0.0.1:8765 <user>@<raspberry-pi>
```

Then launch:

```text
cd laptop  # from the repository root
shakepie-client
```

Use `shakepie-client --capture received.frames` for an optional local copy of validated live frames. It is not a replacement for the Pi HDD archive.

## Recordings

At 750 groups/s, raw payload is 12 kB/s; with version-1 block framing at 64 groups/block, a full day is about 1.11 GB (1.03 GiB). Default rotation is UTC-aligned every 15 minutes. Active files end in `.partial`; completed files end in `.spb`; recovered files end in `.recovered.spb`.

Inspect or export raw values:

```text
shakepie-inspect recording.spb
shakepie-inspect recording.spb --csv recording.csv
```

CSV is only an interchange export. The CRC-protected binary file remains authoritative.

## Deployment script

After configuring SSH and prerequisites, run from PowerShell only after reviewing the target and explicitly choosing to deploy:

```powershell
./deploy/Deploy-ShakePie.ps1 -HostName raspberrypi.local -UserName operator
```

The script checks SSH, uploads source, builds, runs hardware-independent tests, stops the service only after tests pass, installs a versioned release, atomically updates `current`, starts and verifies health, displays logs, and restores the prior symlink if verification fails. It does not install packages or change the HDD mount.
