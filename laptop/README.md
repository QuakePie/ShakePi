# ShakePie laptop client

Install with `python -m pip install -e .` and launch with `shakepie-client`. The TCP endpoint defaults to the local end of the documented SSH tunnel.

Settings are persisted as JSON under the platform configuration directory, or at the path passed with `--config`. Channel labels, polarity, and independent velocity/displacement/acceleration-per-volt scales are configurable there. The window, FFT size, overlap, frequency limits, colormap, and dB limits are also persistent; restart the client after editing those structural settings. Visible duration, display units, time zone, pause, pan, and zoom are available at runtime.

In raw-count mode, spectrogram magnitude is displayed in dBFS relative to the ADS1256 signed 24-bit full scale. Other calibrated modes use dB relative to one displayed unit. This keeps the default `-120..-20 dB` color range useful without changing authoritative raw samples.
