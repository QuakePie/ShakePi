# Tracked implementation plan

This historical plan was written when ShakePie lived inside a parent repository.
The legacy `Software/` reference is not included in this standalone checkout.

- [x] 1. Protocol, strict C++/Python codecs, and cross-language golden fixtures
- [x] 2. Hardware interfaces and deterministic fake ADC
- [x] 3. ADS1256 reset, validation, calibration, timing, and Figure-19 scan driver
- [x] 4. Allocation-free acquisition and bounded disk/live queues
- [x] 5. HDD-only recording, rotation, sync, footer, recovery, inspection, and conversion — Linux tests and simulated end-to-end run passed
- [x] 6. Loopback-only nonblocking stream server with bounded per-client queues — Linux tests passed
- [x] 7. Laptop receiver, frame parser, reconnect, capture, calibration, and rolling buffers
- [x] 8. PySide6/PyQtGraph waveform and incremental ShortTimeFFT user interface
- [x] 9. Hardened systemd unit and idempotent SSH deployment/rollback script (created and parsed, not deployed)
- [x] 10. Full hardware-independent validation — Windows, WSL Linux, ASan/UBSan, static analysis, and simulated end-to-end gates passed

Every phase closes only after its targeted tests pass. Physical wiring, real timing, power-loss behavior, thermal behavior, and soak reliability remain pending until measured on the target hardware.
