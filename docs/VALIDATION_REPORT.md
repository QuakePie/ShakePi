# Software validation report — 2026-09-03

## Completed on the Windows development host

- MSVC C++20 Release build: protocol, portable ADS1256 state machine, fake ADC, acquisition/queues, health/config, file codecs, inspector, and tests built successfully.
- CTest: 3/3 targets passed (`protocol`, `ads1256`, `core`).
- Live overwrite-ring concurrency stress: 20 consecutive 100,000-push runs passed with strictly increasing delivered sequences.
- Python: 33/33 pytest tests passed on the installed Python 3.12+ environment.
- Ruff: passed with no findings.
- Strict mypy: passed for all 10 package modules.
- Qt: the pytest suite created an offscreen `MainWindow`, started both workers, saved configuration, and shut them down cleanly.
- PowerShell deployment script: parsed successfully; it was not executed.

## Completed in Ubuntu 22.04 under WSL2

- GCC 11 C++20 Release build: the complete recorder, Linux spidev/libgpiod 1.6 compatibility path, POSIX storage/recovery, loopback socket server, health CLI, inspector, and tests built without compiler warnings.
- CTest Release: 5/5 targets passed (`protocol`, `ads1256`, `core`, `storage`, `stream`).
- CTest ASan/UBSan: the same 5/5 targets passed with no sanitizer finding.
- clang-tidy 14: the full Linux production sources built with the configured bugprone, analyzer, concurrency, performance, portability, and nullptr checks promoted to errors.
- Simulated end to end after HDD-path hardening: a three-second fake-ADC run stopped cleanly, drained its lossless queue, finalized a recording with 36 blocks/2,225 samples, reported four intentional fake sequence gaps and zero storage/stream errors, and left no partial file.
- The Windows and Linux inspector executables both decoded that same WSL-produced file as station `E2E-WSL`, gain 64, 7,500 SPS ADC rate, and 2.5 V reference. The health CLI decoded the final `stopped`/`none` status.
- The shared golden fixture and codec tests passed in both MSVC and Linux builds, establishing byte-level cross-platform reader compatibility; a separate earlier Linux inspection smoke test also exported all 3,053 rows from a 3,053-sample finalized recording.
- A CMake install smoke test populated the recorder, health and inspection executables, default configuration, and complete documentation tree under a local prefix.

## Not executed on target hardware

There was no Raspberry Pi, ADS1256 board, sensor, production HDD, or remote host in scope. Real SPI/GPIO transactions, GPIO-line permissions, the systemd unit, SSH deployment/tunnel, physical wiring/timing, forced power interruption, thermal behavior, and 24/72-hour soak tests remain pending in `HARDWARE_VALIDATION.md`. WSL's Python is 3.10, so the Python 3.12 GUI suite was run on Windows rather than duplicated in WSL. No service was installed or enabled and no remote system was contacted or changed.
