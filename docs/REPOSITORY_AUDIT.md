# Legacy audit and migration basis

This historical audit describes the predecessor in the original parent repository;
its `Software/` sources are not part of this standalone checkout.

## Existing architecture

The parent repository contains a bcm2835-based C recorder under `Software/RaspberryCode`. It forks: the child busy-waits on DRDY, cycles three differential inputs, converts samples to floating-point voltages, formats CSV-like text, and writes it to a pipe; the parent writes text to five-minute files. Configuration comes from a small parameter file.

## Reusable knowledge

- ADS1256 register and command values.
- Gain and data-rate register mappings.
- Differential MUX values `0x01`, `0x23`, and `0x45`.
- Signed 24-bit extension logic.
- Legacy board defaults: RESET BCM18, manual CS BCM22, DRDY BCM17.

The unbounded DRDY waits, routine formatting and floating-point work in acquisition, unchecked pipe and file operations, `CLOCK_REALTIME` duration calculation, text archive, and ambiguous pipeline semantics are not reused.

## Hardware facts requiring physical confirmation

- Waveshare High-Precision AD/DA board with 7.68 MHz crystal and 2.5 V reference.
- `/dev/spidev0.0` is compatible with manual CS BCM22 when `SPI_NO_CS` is enabled.
- Actual RESET, DRDY, and CS gpiochip offsets on the installed Pi/overlay.
- N/E/Z sensor-to-input mapping and polarity.
- Geophone signal common-mode range and maximum amplitude at the selected PGA.
- Stable HDD mount path, filesystem, ownership, sustained latency, and power behavior.
- Achieved group rate and jitter under the target kernel and workload.

## Migration risks and strategy

The main risks are incorrect wiring/polarity, confusing the sequential axes with simultaneous measurements, GPIO scheduling latency, accidental SSD writes after an unmount, incomplete shutdown writes, slow clients, and overly optimistic throughput. ShakePie remains alongside the legacy recorder. Validate with the fake ADC, then stop the legacy process, run hardware mapping/rate tests, perform shadow comparisons into a separate HDD directory, and only then enable the new service. Versioned release directories and the `current` symlink make rollback independent of recorded data.

Reference basis: the [TI ADS1256 Rev. K datasheet](https://www.ti.com/document-viewer/ADS1256/datasheet) (especially Figure 19, t6/t11, and Table 14), the [Waveshare High-Precision AD/DA Board documentation](https://www.waveshare.com/wiki/High-Precision_AD/DA_Board), its [reference source](https://github.com/waveshareteam/High-Precision-AD-DA-Board), and the [official libgpiod API](https://kernel.googlesource.com/pub/scm/libs/libgpiod/libgpiod/). The TI table gives 3043 multiplexed conversions/s at a 7500 SPS setting, an ideal upper bound of about 1014 complete three-channel groups/s before Linux and SPI overhead.
