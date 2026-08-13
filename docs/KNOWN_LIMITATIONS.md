# Known limitations

- N, E, and Z are sequential ADS1256 multiplexer conversions, not simultaneous samples. The installed Pi/kernel must demonstrate the required group rate and jitter; libgpiod/spidev is not claimed to be hard real-time.
- Sequence numbers restart when the recorder process restarts. The laptop recognizes a changed-timestamp sequence reset on the first block of a new TCP connection and reports a recorder restart, but protocol v1 has no explicit session UUID.
- Physical-unit modes apply independent, configured per-channel scale and polarity factors. They do not perform instrument-response deconvolution, differentiation, or integration; calibrated factors must come from the actual geophone and analog front end.
- Structural spectrogram settings are persisted in JSON and take effect on client restart. Visible duration, units, pause, pan, zoom, and UTC/local display are runtime controls.
- The live protocol has no TLS or application authentication because it is constrained to loopback and intended for an SSH tunnel. Do not expose it through a public proxy.
- This WSL run compiled the libgpiod 1.6.3 branch. The conditional libgpiod 2.x implementation was checked against the upstream API but still needs a compile/run gate on a host that supplies libgpiod 2.x.
- WSL2 validation covers Linux compilation against libgpiod, POSIX storage/recovery, sockets, sanitizers, static analysis, and a fake-ADC end-to-end run. It cannot validate Raspberry Pi SPI/GPIO hardware, real HDD/power-loss behavior, systemd boot behavior, SSH deployment, timing under Pi load, or physical/soak checks; those remain pending in `VALIDATION_REPORT.md` and `HARDWARE_VALIDATION.md`.
