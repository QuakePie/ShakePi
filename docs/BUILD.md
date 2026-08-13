# Build, test, and install

## Raspberry Pi prerequisites

On current Raspberry Pi OS, install the normal build dependencies explicitly (the deployment script does not alter packages):

```text
sudo apt update
sudo apt install build-essential cmake ninja-build libgpiod-dev
```

Build all Pi targets and hardware-independent tests:

```text
cd ShakePi
cmake -S . -B build/pi -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSHAKEPIE_BUILD_PI=ON -DSHAKEPIE_BUILD_TESTS=ON
cmake --build build/pi --parallel
ctest --test-dir build/pi --output-on-failure
sudo cmake --install build/pi --prefix /opt/shakepie/releases/manual
```

For a non-Pi sanitizer build where libgpiod and Linux spidev headers are present:

```text
cmake -S . -B build/asan -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DSHAKEPIE_BUILD_PI=ON -DSHAKEPIE_BUILD_TESTS=ON \
  -DSHAKEPIE_ENABLE_SANITIZERS=ON
cmake --build build/asan --parallel
ctest --test-dir build/asan --output-on-failure
```

Set `SHAKEPIE_ENABLE_CLANG_TIDY=ON` for the configured clang-tidy checks.

## Windows portable C++ validation

```text
cmake -S . -B build/windows -A x64 -DSHAKEPIE_BUILD_PI=ON -DSHAKEPIE_BUILD_TESTS=ON
cmake --build build/windows --config Release --parallel
ctest --test-dir build/windows -C Release --output-on-failure
```

Windows builds the protocol, codecs, file inspector, ADS state machine, fake ADC, and queue/acquisition tests. Linux-only storage, spidev/libgpiod, socket, and service targets are built in Linux CI and on the Pi.

## Laptop package

Python 3.12 or newer is required. From `laptop/` at the repository root, use the commands for the host OS.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check src tests ../protocol/generate_fixture.py
& .\.venv\Scripts\python.exe -m mypy src
& .\.venv\Scripts\shakepie-client.exe
```

Linux:

```text
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests ../protocol/generate_fixture.py
.venv/bin/python -m mypy src
.venv/bin/shakepie-client
```
