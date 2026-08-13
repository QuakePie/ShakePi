# Architecture

## Recorder

The recorder preallocates every acquisition and queue slot before starting the sampling thread:

```text
ADS1256 / deterministic fake ADC
        |
        v
allocation-free acquisition thread
        | copy                          | copy
        v                               v
bounded lossless SPSC queue       bounded overwrite ring
        |                               |
        v                               v
storage thread                     stream thread
128 KiB aggregation                per-client bounded queues
        |                               |
mounted HDD (.partial -> .spb)     127.0.0.1 TCP -> SSH
```

The disk push happens first. A full disk queue sets a fatal health code and requests an orderly stop; it never silently discards a block. The live producer does not wait: it uses an empty preallocated slot or reclaims the oldest ready slot, never one currently read by the stream thread. Every live loss is visible as a sequence gap and counter.

The HDD data directory must already exist on the verified mount. For each active file, storage opens and rechecks that directory, confirms its device matches the mount, keeps the directory descriptor open, and uses `openat`, `renameat`, `fstatvfs`, and descriptor `fsync`. Normal unmount cannot redirect a pathname reopen to the OS disk while recording.

A failed source read advances the logical sample sequence, forces a block boundary when necessary, and increments `missing_sample_groups`; a later successful block therefore exposes the loss as a sample-sequence gap. Once the hardware ADS1256 pipeline is desynchronized, subsequent immediate fault reports do not invent additional timed gaps, and the configured consecutive-fault limit stops acquisition.

ADS1256 acquisition follows datasheet Figure 19. After reset, ID 3 validation, configuration, and self-calibration, an initial conversion is explicitly discarded to prime the pipeline. On each subsequent DRDY event, the driver writes the next differential MUX selection, sends `SYNC`, waits at least t11, sends `WAKEUP`, waits, and reads the previous conversion after t6. N, E, Z map to AIN0–AIN1, AIN2–AIN3, AIN4–AIN5. These conversions are sequential, not simultaneous.

`CLOCK_MONOTONIC_RAW` supplies intervals and per-group block offsets. Each block also records a midpoint-mapped `CLOCK_REALTIME` value for UTC. A difference between real and monotonic deltas over 100 ms sets the UTC-step timing flag.

The stream server is a thread in the recorder process rather than a second service. Its failure cannot stop storage or acquisition, while keeping the live handoff in preallocated memory avoids another IPC queue and failure surface.

## Laptop

The receiver thread performs socket I/O, strict framing/CRC validation, sequence accounting, optional local frame capture, and bounded reconnect backoff. It places validated blocks on a bounded queue. The processing thread performs NumPy conversion, calibration, rolling-buffer maintenance, and incremental `scipy.signal.ShortTimeFFT` work. A two-element overwrite queue hands snapshots to the Qt UI at 25 Hz.

Pausing affects rendering only. Reception, gap accounting, buffering, and spectral processing continue. The UI decimates each waveform to approximately the visible pixel width with min/max buckets so impulses remain visible.

## Failure policy

- Missing HDD mount: refuse startup; never fall back to the OS filesystem.
- Disk queue full, write/sync/close/rename error, repeated ADC failure: fatal health state and orderly stop.
- Power interruption: previously renamed files remain independent; startup scans and truncates only an incomplete final record in each `.partial` file.
- No/slow/disconnected client: acquisition and storage continue; live blocks may be dropped with observable gaps.
- Stream bind failure: health counter and journal error; recording continues.
