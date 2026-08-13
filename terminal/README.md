# ShakePie terminal console

`shakepie-terminal` is an interactive SSH console for the ShakePie recorder. It runs in any
terminal on Windows, macOS, or Linux and lets you offload data, manage recordings and dayplots,
and change recorder settings — navigating entirely with the arrow keys.

## Install

Python 3.12 or newer is required.

```text
cd terminal  # from the repository root
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
.venv/bin/python -m pip install -e ".[dev]"       # Linux/macOS
```

The console script is `shakepie-terminal`; `python -m shakepie_terminal` is equivalent.

## Run

```text
shakepie-terminal --host raspberrypi.local --user <your-pi-user>
```

You are asked for the recorder's SSH password once per session (it is kept in memory only and
never written to disk). Host, user, port, and the download directory are remembered in
`~/.shakepie-terminal.json`; a recorder with SSH keys set up needs no password at all.

## What it can do

- **Status and health** — recorder state, counters, HDD usage, SoC temperature, service states,
  the event-detector trigger mode (vertical-only or N-of-M coincidence).
- **Recordings** — batch-download whole UTC days with progress bars; or download them
  **converted to miniSEED** (all components or a single one — conversion runs on the recorder,
  so ObsPy, Swarm, and jAmaSeis read the files immediately); "download everything not
  yet offloaded" skips days already present locally, so re-running an offload is idempotent.
  Permanent deletion is day-based and requires typing `DELETE`; live `.partial` files are never
  touched, so it is safe while the recorder runs.
- **Dayplots** — batch download (or delete) the generated daily plot images, preserving the
  day subfolders; ideal for uploading them to a website afterwards.
- **Storage summary** — recording/dayplot sizes and free space on the HDD.
- **Sensor zero calibration** — measure the DC offset of each channel over a short
  window and subtract it so data is recorded around 0; offsets persist on the Pi and
  survive restarts. The Status view shows the applied offsets.
- **Recorder settings** — edit the ADC conversion rate, sample-group rate, groups per block,
  ADC error tolerance, rotation interval, and station ID. Values are validated against the
  recorder's own configuration rules before anything is written; the current file is backed up
  on the Pi (`recorder.conf.bak.<stamp>`) and the recorder restarts after a successful apply.
- **Logs** — the last 60 journal lines of the recorder, web interface, and dayplot worker.

## Key bindings

| Key | Action |
| --- | --- |
| ↑ / ↓ (or k / j) | Move |
| Enter | Select / confirm |
| Space | Toggle a selected day (batch lists) |
| a / n | Select all / none (batch lists) |
| q, Esc, Backspace | Back |
| Ctrl-C | Quit |

## Scripted (non-interactive) actions

Every bulk operation is also available for automation, without menus:

```text
shakepie-terminal --action status
shakepie-terminal --action list-days
shakepie-terminal --action download-days --days 2026-09-14,2026-09-15 --local-dir D:\Offload
shakepie-terminal --action download-miniseed --days 2026-09-15 --channel all --local-dir D:\Offload
shakepie-terminal --action calibrate      # recenter data around 0 (measure ~10 s)
shakepie-terminal --action clear-zero     # record raw counts again
```

For unattended runs, provide the password through the `SHAKEPIE_TERMINAL_PASSWORD` environment
variable instead of the prompt. `download-days` skips files that already exist locally with the
right size, so scheduled runs only fetch what is new; `download-miniseed` skips days whose
`.miniseed` files are already present.

## Safety notes

- Deleting recordings requires typing `DELETE` and only removes finalized `.spb` files of the
  chosen days; the file that the recorder is currently writing is never a candidate.
- Settings are validated against the same rules the recorder enforces at startup — for example,
  the ADC conversion rate must exceed six times the sample-group rate, because each of the
  three sequential channels needs a settling conversion and a retained conversion.
- The connection profile never stores passwords. Point `--host`/`--user` at any recorder; the
  tool is not tied to one device.
