# Hardware validation checklist

Software tests do not establish physical reliability. Record the Pi model, OS/kernel, ADS1256 board revision, oscillator, reference measurement, geophone models, analog front end, HDD/adapter/filesystem, power supply, enclosure, and ambient conditions for every run.

## Bring-up

- [ ] Confirm RESET, CS, DRDY, SPI bus, and gpiochip offsets.
- [ ] Measure VREF and verify input common-mode/PGA headroom.
- [ ] Inject a known positive signal into each pair and confirm N/E/Z mapping and polarity.
- [ ] Confirm AIN0–1, AIN2–3, AIN4–5 order with a logic analyzer and test signals.
- [ ] Verify the first delivered conversion after priming belongs to N.
- [ ] Measure actual three-channel group rate and distribution of monotonic inter-group intervals.
- [ ] Record mean, standard deviation, percentiles, maximum jitter, late flags, and CPU load.

## Fault and recovery

- [ ] Disconnect/reconnect the laptop repeatedly while comparing the HDD sequence.
- [ ] Stop/restart only the UI/tunnel and confirm uninterrupted recording.
- [ ] Exercise a slow client until live queues drop data; verify explicit gaps and no disk gaps.
- [ ] Fill a test filesystem and inject EIO/slow-HDD behavior; confirm fatal state and no silent loss.
- [ ] Unmount or disconnect the HDD; confirm no file appears on the OS SSD.
- [ ] SIGTERM during acquisition; verify queue drain, footer, sync, rename, and restart.
- [ ] SIGKILL/power interruption at several points in the sync window; recover `.partial`, validate CRCs, and bound loss to the configured window.
- [ ] Reboot the Pi and confirm the mount/service dependency prevents fallback.
- [ ] Corrupt the active partial and verify older finalized files remain readable.
- [ ] Exercise a backwards/forwards UTC step and confirm monotonic intervals remain valid.

## Soak tests

- [ ] Complete a 24-hour run before unattended use.
- [ ] Complete a 72-hour run after the 24-hour run passes.
- [ ] Report CPU percent, resident memory and growth, temperature, throttling flags, disk free space, data volume, disk write-latency percentiles/max, disk-queue high-water mark, live drops, sample/block gaps, ADC timeouts/faults, restarts, and recovery outcome.
- [ ] Verify every finalized file and compare total sample sequences across rotations.
- [ ] Document unexplained anomalies; do not mark hardware validation complete while any remain.
