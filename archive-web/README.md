# ShakePie archive web interface

This package provides an authenticated Flask site for browsing finalized ShakePie recordings,
streaming individual downloads, permanently deleting confirmed recordings, requesting cached
ObsPy dayplots, viewing the recorder's live stream, analyzing an individual recording, and
editing validated recorder settings. It is independent of recorder acquisition and never treats
`.partial` files as authoritative archives.

## What the interface provides

- **Overview** — recorder, storage, index, dayplot, event-detector health, host metrics (CPU
  load, SoC temperature, memory, uptime), and the latest daily plot thumbnail.
- **Archive** — per-UTC-day recording tables with downloads, analysis links, and permanent
  deletion; a **calendar picker** highlights every day that contains recordings
  (`/api/days` feeds the picker on the Archive, Dayplot, and Recorded pages).
- **Dayplot** — on-demand helicorder-style plots; **completed UTC days are queued
  automatically** by the worker (`auto_generate_days`), so a finished day always ends up with
  its daily plot. Clicking a plot line opens the recorded data covering that time of day
  (coarse, one helicorder row).
- **Events** — automatic **STA/LTA detection** on the live recorder stream. By default the
  vertical channel triggers alone (`[detector] minimum_components = 1`); with `2` or `3` the
  N-of-M **coincidence trigger** requires that many components to trigger simultaneously, so
  single-channel bumps and electrical spikes never reach the event list.
  Finished events (start, duration, peak amplitude, trigger ratio) are stored in the catalog
  and listed with links to the covering recording. Configuration lives in the `[detector]`
  section of `archive-web.conf`; the detector keeps one persistent loopback stream client and
  clears its windows on recorder restarts.
- **Live data** — the recorder's loopback stream with waveform and spectrogram canvases whose
  time axes are exactly aligned; the spectrogram color legend lives in the page legend strip,
  outside the plot area, so it never changes plot widths.
- **Kiosk** (`/kiosk`) — chrome-less dark wall display: three live waveform lanes, UTC +
  Beijing clock, recorder state, and a disk/events status footer, with a fullscreen button.
- **Play as sound** — on the Recorded data page, any loaded recording can be played as
  audible audio: pick North, East, or Vertical and press play. The data is sped up ~40x
  into the audible range, peak-normalized to a safe level (so every recording plays at
  the same loudness and phone speakers are never overdriven), and served as a WAV.
- **Recorded data** — per-recording waveform envelopes and spectrograms, also sharing aligned
  axes, with the **measured effective sample rate** shown next to the stored nominal rate.
  The page accepts an `at=HH:MM:SS` query parameter and selects the recording covering that
  time; selections sync back into the URL so views can be shared. Two one-click analyses run
  on demand: **Particle motion** (North–East hodogram with polarization azimuth and linearity
  for a chosen window) and **H/V ratio** (Nakamura ambient-noise spectral ratio with the site
  resonance peak marked). **Export MiniSEED** downloads the recording as standard miniSEED
  2.4 (EHN/EHE/EHZ, lossless INT32, gaps split into truthful new series) that opens directly
  in ObsPy, Swarm, or jAmaSeis.
- **Event notifications (Bark)** — every detected event is pushed to an iPhone via the Bark app (`[notify]` section; the device key lives in the protected env file as `SHAKEPIE_NOTIFY_BARK_KEY`). Delivery runs on its own thread with a rate limit, so trigger storms cannot flood the phone or slow the detector.
- **Earthquake watch** — watch points poll USGS, ISC, and/or CENC (the China Earthquake
  Networks Center feed) for quakes inside a radius; matches are listed on the Watch page and
  pushed via Bark.
- **Sensor zero calibration** — Settings page button that recenters recorded data around 0: the recorder measures each channel's mean over a short window while recording continues, applies the offsets atomically, and persists them across restarts (`/var/lib/shakepie`).
- **Settings** — validated recorder administration (ADC rate, group rate, samples per block,
  rotation, station ID). Values are checked against the recorder's own rules, the current file
  is backed up, the new file is written atomically, and services restart over the systemd
  D-Bus API with a polkit rule (see *Recorder settings page* below).
- **Dark mode** — follows the system preference by default; the topbar toggle overrides it and
  the choice persists in the browser. Plot canvases follow the active theme.

## Daily plot naming and automation

Every generated dayplot uses the canonical, website-ready name

```text
<YYYYMMDD>-<STATION>-CH<n>.png     e.g. 20260914-Caspian-CH3.png
```

inside `dayplots/<UTC-day>/`. `CH1`/`CH2`/`CH3` are the north/east/vertical channels, and
`<STATION>` is the recorder's configured station ID (`storage.station_id`, edited on the web
settings page) — renaming the station only affects newly generated files. Regeneration
overwrites the same name atomically. On startup the dayplot worker migrates older
`<fingerprint>-<channel>.png` and `STATION_YYYYMMDD_channel_dayplot.png` files to canonical
names and removes unreferenced leftovers. The Dayplot page also has a "Regenerate all days"
button that queues a fresh job for every archived day; the worker plots them one by one.
`deploy/download-dayplots.bat` (Windows), `deploy/download-dayplots.ps1` (PowerShell), and
`deploy/download-dayplots.sh` (Linux/macOS) copy all plots from the Pi into a local
`dayplots/` folder for upload to a website.

## Recorder settings page

The Settings page edits `adc.data_rate_sps`, `acquisition.nominal_group_rate_hz`,
`acquisition.samples_per_block`, `acquisition.max_consecutive_adc_errors`,
`storage.rotation_minutes`, and `storage.station_id` in `/etc/shakepie/recorder.conf`. The
web service validates against the same rules as the recorder's C++ configuration parser — in
particular the ADC rate must exceed three times the group rate because every N/E/Z group needs
three sequential conversions. Applying a change: backs up the file (`recorder.conf.bak.<stamp>`),
writes the replacement atomically, updates `[live] sample_rate_hz` and
`[dayplot] target_sample_rate_hz` in `archive-web.conf` when the rate changed, and restarts
`seisberry-recorder.service` (plus the dayplot worker, and the web service itself when the rate
changed) over the systemd D-Bus API. Acquisition pauses during the restart and sequence
numbers start from zero; existing recordings are untouched.

Deployment requirements for the settings page:

```text
# /etc/polkit-1/rules.d/49-shakepie-web-restart.rules (root-owned):
# allow the web account to restart exactly these three units via systemctl/D-Bus.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "shakepie-web") {
        var unit = action.lookup("unit");
        if (unit == "seisberry-recorder.service" ||
            unit == "shakepie-dayplot-worker.service" ||
            unit == "shakepie-archive-web.service") {
            return polkit.Result.YES;
        }
    }
});

# both configuration files are group-writable by the web account, and the directory
# must accept the timestamped backup files
sudo chgrp shakepie /etc/shakepie/recorder.conf /etc/shakepie/archive-web.conf
sudo chmod 0660 /etc/shakepie/recorder.conf /etc/shakepie/archive-web.conf
sudo chmod 0770 /etc/shakepie
```

The shipped `shakepie-archive-web.service` deliberately keeps `/etc/shakepie` out of
`ReadOnlyPaths` (systemd applies `ReadOnlyPaths` after `ReadWritePaths`, so a path in both
would stay read-only); the root-only `archive-web.env` secrets remain unreadable to the web
account regardless. `NoNewPrivileges=true` stays enabled because restarts use D-Bus, not sudo.

## Architecture and resource boundary

- Gunicorn runs one Flask worker with two threads. Pages are server rendered. Live polling occurs
  only while the Live data page is open; its recorder connection closes after an idle timeout.
- SQLite contains metadata, jobs, cache records, and audit entries—not seismic
  samples. The catalog is rebuildable by scanning finalized files.
- Reconciliation validates file/header/footer CRC32C, every block CRC32C and layout, block order,
  sample order, and sequence gaps in bounded memory.
- Downloads use a catalog ID and an open file descriptor. The indexed device, inode, size, and
  modification time must still match; symlinks and non-regular files are rejected.
- Permanent deletion revalidates the catalog-selected file identity and requires a confirmation
  dialog carrying the exact filename, preventing a stale or mismatched row from deleting a file.
- The worker claims one SQLite job at a time. It launches a short-lived generator that processes
  North, East, and Vertical sequentially. Each channel uses an HDD-backed float32 memory map and
  byte mask so missing samples remain gaps. ObsPy and Matplotlib are imported only in that process.
- Current-day dayplots are snapshots of finalized files available at generation time. The complete
  24-hour UTC axis is retained and active or missing periods remain masked and blank.
- The live reader validates the recorder's CRC-framed loopback stream and retains only a configured
  short block history. It sends the configured 200 Hz view; the
  browser reuses six canvases and performs the incremental display FFT.
- Individual-recording analysis validates a catalog-selected file and streams through it once.
  Only bounded waveform envelopes and time-compressed spectrogram columns reach the browser.

The production template listens on TCP port 8050 for direct access from a trusted local network.
Direct public internet exposure is unsupported; plain HTTP credentials must not cross an untrusted
network.

## Local development and tests

Python 3.12 is required.

```text
cd archive-web  # from the repository root
python -m venv .venv
.venv/bin/python -m pip install -e ".[dayplot,production,dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python`.

For a disposable development configuration, set `require_mount = false`, use absolute paths under a
temporary directory, and export both required secrets. Do not run Flask debug mode.

## Raspberry Pi installation

The recorder and archive service use separate non-root accounts. The `shakepie-web` account joins
the recorder's `shakepie` group so it can read and delete finalized files; it does
not receive SPI or GPIO access.

1. Install OS prerequisites (package names shown for current Raspberry Pi OS/Debian):

   ```text
   sudo apt update
   sudo apt install -y python3-venv python3-dev build-essential libopenblas-dev liblapack-dev \
     libfreetype6-dev libpng-dev
   ```

2. Create the account and HDD directories after verifying the mount:

   ```text
   findmnt --mountpoint /mnt/shakepie-data
   sudo useradd --system --gid shakepie --home-dir /nonexistent \
     --shell /usr/sbin/nologin shakepie-web
   sudo install -d -o shakepie -g shakepie -m 2770 /mnt/shakepie-data/recordings
   sudo install -d -o shakepie-web -g shakepie -m 2750 \
     /mnt/shakepie-data/index \
     /mnt/shakepie-data/dayplots /mnt/shakepie-data/dayplots/tmp
   ```

   Confirm the recorder can still create and finalize a test recording after changing directory
   permissions. Never deploy before checking that `/mnt/shakepie-data` is the real HDD mount.

3. Copy this directory to `/opt/shakepie/archive-web`, create the environment, and install:

   ```text
   sudo install -d -o root -g root -m 0755 /opt/shakepie/archive-web
   sudo cp -a ShakePie/archive_web/. /opt/shakepie/archive-web/
   sudo python3 -m venv /opt/shakepie/archive-web/.venv
   sudo /opt/shakepie/archive-web/.venv/bin/python -m pip install --upgrade pip
   sudo /opt/shakepie/archive-web/.venv/bin/python -m pip install \
     -e '/opt/shakepie/archive-web[dayplot,production]'
   ```

4. Install configuration and create secrets. The password command prints only a hash:

   ```text
   sudo install -d -o root -g shakepie -m 0750 /etc/shakepie
   sudo install -o root -g shakepie -m 0640 \
     /opt/shakepie/archive-web/deploy/archive-web.conf /etc/shakepie/archive-web.conf
   /opt/shakepie/archive-web/.venv/bin/shakepie-archive-password
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   sudoedit /etc/shakepie/archive-web.env
   sudo chown root:root /etc/shakepie/archive-web.env
   sudo chmod 0600 /etc/shakepie/archive-web.env
   ```

   Put the generated values in `SHAKEPIE_ARCHIVE_PASSWORD_HASH` and
   `SHAKEPIE_ARCHIVE_SECRET_KEY`. Shell-quote the password hash because it may contain `$`.
   The systemd manager reads the root-only environment file before starting the service; the
   application reads its non-secret configuration through the service's `shakepie` group.

5. Install the units and build the first catalog before starting the site:

   ```text
   sudo install -m 0644 /opt/shakepie/archive-web/deploy/shakepie-archive-*.service \
     /opt/shakepie/archive-web/deploy/shakepie-archive-reconcile.timer \
     /opt/shakepie/archive-web/deploy/shakepie-dayplot-worker.service \
     /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl start shakepie-archive-reconcile.service
   ```

6. Start and enable the services:

   ```text
   sudo systemctl enable --now shakepie-archive-web.service
   sudo systemctl enable --now shakepie-dayplot-worker.service
   sudo systemctl enable --now shakepie-archive-reconcile.timer
   ```

## Browser access

The production configuration binds Gunicorn to `0.0.0.0:8050`. Normal use does not require SSH.
Open either address from another device on the same trusted LAN:

```text
http://raspberrypi.local:8050
http://<raspberry-pi-ip>:8050
```

The lightweight unauthenticated liveness check is `http://<raspberry-pi-ip>:8050/health`; it returns no
recorder metadata or secrets. All archive, live-data, download, plotting, and deletion routes remain
session-authenticated.

Keep authentication enabled and restrict TCP port 8050 to the local subnet with the host/router
firewall when a firewall is active. Set `session_cookie_secure = true` only behind HTTPS.

## Plot configuration

`archive-web.conf` contains bounded controls for both interactive plotting paths:

- `[live]` selects the loopback recorder host/port, retained history, browser refresh interval,
  idle disconnect, and default maximum spectrogram frequency.
- `[recorded_plot]` controls the waveform envelope count, spectrogram time columns, FFT size, and
  default maximum frequency. The shipped maximum is 100 Hz for both views.

## Operations and upgrades

```text
sudo systemctl status shakepie-archive-web shakepie-dayplot-worker
sudo systemctl list-timers shakepie-archive-reconcile.timer
sudo journalctl -u shakepie-archive-web -n 100 --no-pager
sudo journalctl -u shakepie-dayplot-worker -n 100 --no-pager
sudo journalctl -u shakepie-archive-reconcile -n 100 --no-pager
```

For an upgrade, stop only the two web-owned services (not the recorder), replace the source, rerun
the pip install command, reconcile the catalog, and restart the services. Keep the SQLite file and
cached output on the HDD. Back up `/etc/shakepie/archive-web.env` separately and protect it as a
secret.

## Raspberry Pi validation still required

- Confirm mount checks, ownership, and atomic rename behavior on the actual HDD filesystem.
- Measure recorder disk latency and queue depth during a download, reconciliation, deletion,
  and a full-rate source dayplot job.
- Measure generator peak RSS and temporary disk use; tune `memory_limit_mib`, `MemoryMax`, image
  width, and minimum free space for the Pi model.
- Verify `ionice`, `Nice`, CPU/IO weights, and systemd hardening on the installed OS release.
- Validate PNG output and gaps from a known 24-hour recording, including recovered files.
- Exercise power loss/restart while a job is Running and during a confirmed deletion.

The dayplot-only `target_sample_rate_hz` setting is 200 Hz. Historical higher-rate recordings are
placed onto that fixed UTC timeline before a bounded min/max display envelope is passed to ObsPy.
This keeps gaps masked, retains visible excursions, and avoids ObsPy deep-copying a high-rate full-day
array. Raw recordings are never resampled or modified.
