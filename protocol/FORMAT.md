# ShakePie binary format, version 1

All integers are little-endian. Native C/C++ structures are never written. A file reader must reject unknown versions, noncanonical lengths, sample counts outside 1–256, layouts other than three 32-bit channels plus one 32-bit time offset, unsupported channel-order codes, and bad CRC32C values.

## Sample block

The 64-byte block header is followed by `sample_count * 16` payload bytes and a four-byte CRC32C (Castagnoli polynomial). The CRC covers the header and payload, not the CRC field.

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | char[4] | `SPBK` |
| 4 | u16 | protocol version (`1`) |
| 6 | u16 | header length (`64`) |
| 8 | u32 | payload length |
| 12 | u32 | status flags |
| 16 | u64 | block sequence |
| 24 | u64 | first sample-group sequence |
| 32 | u64 | UTC start, Unix nanoseconds |
| 40 | u64 | `CLOCK_MONOTONIC_RAW` start, nanoseconds |
| 48 | u32 | sample-group count |
| 52 | u32 | timing flags |
| 56 | u32 | nominal sample-group rate in millihertz |
| 60 | u16 | sample-group length (`16`) |
| 62 | u8 | channel count (`3`) |
| 63 | u8 | channel order (`0` = north, east, vertical) |

Live-drop totals are carried by health telemetry and are also inferable from sequence gaps.

Each payload record is:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | i32 | raw north count, AIN0–AIN1 |
| 4 | i32 | raw east count, AIN2–AIN3 |
| 8 | i32 | raw vertical count, AIN4–AIN5 |
| 12 | u32 | nanoseconds from block monotonic start |

Raw ADC values occupy the signed 24-bit range inside `int32`. Calibration is deliberately absent from authoritative samples. Within a block, offsets are nondecreasing, the nominal rate is nonzero, and `first_sample_sequence + sample_count - 1` must fit in `u64`.

## Recording file

A recording starts with a 128-byte file header, contains a sequence of `u32 frame_length` plus exact `SPBK` blocks, and ends with a 64-byte footer. Active recordings use `.partial`; only a synced, footered recording is atomically renamed to `.spb`.

### File header (`SPFH`, 128 bytes)

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | char[4] | `SPFH` |
| 4 | u16 | file version (`1`) |
| 6 | u16 | file-header length (`128`) |
| 8 | u16 | block protocol version (`1`) |
| 10 | u16 | block-header length (`64`) |
| 12 | u16 | sample-group length (`16`) |
| 14 | u8 | channel count (`3`) |
| 15 | u8 | channel order (`0` = N/E/Z) |
| 16 | u16 | ADS1256 PGA gain |
| 18 | u16 | file flags (must be `0` in version 1) |
| 20 | u32 | configured ADS data rate, millihertz |
| 24 | u32 | reference voltage, microvolts |
| 28 | u32 | configured groups per block |
| 32 | u64 | creation UTC nanoseconds |
| 40 | u64 | creation monotonic-raw nanoseconds |
| 48 | u64 | first block sequence |
| 56 | u64 | first sample sequence |
| 64 | char[32] | zero-terminated ASCII station ID |
| 96 | byte[28] | reserved, zero |
| 124 | u32 | CRC32C over bytes 0–123 |

Every data record is a little-endian `u32` block length followed by exactly that many block bytes. The accepted block length is 84–4,164 bytes in version 1.

### File footer (`SPFT`, 64 bytes)

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | char[4] | `SPFT` |
| 4 | u16 | file version (`1`) |
| 6 | u16 | footer length (`64`) |
| 8 | u64 | block count |
| 16 | u64 | sample-group count |
| 24 | u64 | first block sequence |
| 32 | u64 | last block sequence |
| 40 | u64 | first sample sequence |
| 48 | u64 | last sample sequence |
| 56 | u32 | footer flags (`bit 0` = recovered partial) |
| 60 | u32 | CRC32C over bytes 0–59 |

Recovery validates the header, then scans framed blocks in sequence. It truncates at the end of the last complete CRC-valid block, appends a recovered footer, calls `fdatasync`, renames within the data directory, and `fsync`s that directory. A damaged header is preserved and stops startup for manual diagnosis. A zero-length file, or a valid header with no complete data block, is preserved as `.partial.empty` and does not prevent the next recording from starting.

Version-1 encoders and decoders require all unused station-ID bytes, header reserved bytes, undefined file flags, and undefined footer flags to be zero. This keeps byte-level compatibility canonical and prevents an older reader from silently ignoring unknown metadata.
