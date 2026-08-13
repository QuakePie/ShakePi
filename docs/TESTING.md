# Test inventory

C++ CTest targets cover explicit block serialization/CRC/golden compatibility, 24-bit sign boundaries, ADS1256 initial discard and N/E/Z MUX cycle, configuration validation, lossless-ring wrap/full behavior, overwrite-ring ordering, block/sample sequencing, visible simulated gaps, UTC clock steps, short writes/EINTR/ENOSPC/EIO, file header/footer validation, partial truncation/recovery, UTC rotation, graceful queue drain, unsolicited-client isolation, and slow-client queue shedding.

Python pytest targets cover the golden fixture, partial and coalesced TCP frames, corrupt/oversized frames, out-of-order sequences and monotonic sample times, reconnect continuity/gap reporting, recorder sequence resets, receiver queue boundaries and capture-write failure isolation, per-channel calibration, rolling-buffer wrap, impulse-preserving decimation, persistent configuration with explicit memory-product limits, bounded incremental `ShortTimeFFT`, gap-safe window resets, count-mode dBFS scaling, epoch-scale spectrogram view bounds, and processing/UI queue boundaries.

The repository-root `.github/workflows/ci.yml` runs on pushes, pull requests, and manual
dispatches. Linux CI builds spidev/libgpiod, POSIX storage, socket server, CLI, and
service-facing recorder code with ASan/UBSan, then runs clang-tidy. Windows CI compiles
and tests the portable reader/core.

Python 3.12 tests, Ruff, and strict mypy run for `laptop`, `archive-web` (including the
dayplot dependencies), and `terminal` on Linux and Windows. The Linux-only OLED service
has its own Linux checks. Qt tests use the offscreen platform and do not require a display.

From each Python package directory, install its dev extras and run:

```text
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m mypy src
python -m pytest -q
```

For `archive-web`, install `.[dev,dayplot]` to exercise plotting tests too. From `laptop`,
also run `python -m ruff check ../protocol/generate_fixture.py`.

Physical tests are intentionally separate in `HARDWARE_VALIDATION.md`.
