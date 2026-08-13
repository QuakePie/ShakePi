# Troubleshooting

## Recorder refuses to start

- `configured HDD path is not an active mount point`: check `findmnt --mountpoint`, `/etc/fstab`, filesystem health, and the service's mount path. Do not create a fallback directory on the SSD.
- `cannot request ADS1256 GPIO lines`: confirm gpiochip offsets, udev groups, and that no legacy recorder holds the lines.
- `unexpected ADS1256 chip ID`: check board power, mode-1 SPI, manual CS wiring, reset/DRDY lines, and SPI speed.
- `pipeline prime timed out`: inspect DRDY with a scope/logic analyzer and verify the 7.68 MHz clock.

## Fatal health state

`disk_queue_full` means storage could not keep up for the configured duration. Preserve all files, inspect HDD latency/errors, free space, USB/SATA power, and kernel logs before restarting. `storage_write` commonly maps to ENOSPC or EIO. `adc_fault` follows the configured number of consecutive timeouts/transport failures and stops rather than risking channel mislabeling.

## Live display gaps

Compare block gaps, sample gaps, receiver queue drops, and Pi status flags. Block-only gaps usually mean a slow network/client and do not imply HDD loss. Sample gaps originate before block creation and require investigation. Validate the authoritative recording independently.

## Spectrogram or waveform concerns

Confirm the configured effective group rate; the ADC's programmed conversion rate is not the three-channel group rate. Inspect raw counts before applying voltage or physical calibration. Calibration scales are display-only and must be established experimentally for each sensor and analog front end.
