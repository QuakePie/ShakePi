# ShakePie live protocol, version 1

The recorder listens on `127.0.0.1:8765` by default. It is intentionally not a public service; access is through an SSH local forward:

```text
ssh -N -L 8765:127.0.0.1:8765 <user>@<raspberry-pi>
```

The server sends a stream of frames. Each frame is a four-byte little-endian unsigned length followed by one exact block defined in `FORMAT.md`. The maximum accepted frame is 4,164 bytes in version 1. A client must accumulate partial TCP reads, may receive multiple frames in one read, and must validate length, version, layout, CRC, block sequence, and sample sequence before publishing samples.

There is no request protocol. Unexpected client input is grounds for disconnecting that client. TCP keepalive detects dead peers; bounded send queues drop their oldest live frames rather than exerting backpressure on acquisition. A gap in `block_sequence` or `first_sample_sequence` is therefore an explicit live-data loss indicator. Disk recordings are independent and authoritative.

The three axes are acquired sequentially through one ADS1256 multiplexer. A logical group is N then E then Z; it is not a simultaneous vector observation. The group timestamp is taken after the third conversion and its intra-block offset is monotonic.

## Compatibility rules

- A sender never changes version-1 field sizes.
- A receiver rejects unsupported versions rather than guessing.
- Future optional data uses a new protocol version or a larger explicitly versioned header.
- CRC32C uses polynomial `0x1EDC6F41` (reflected implementation `0x82F63B78`), initial value `0xffffffff`, and final XOR `0xffffffff`.
- UTC is for display and file rotation. Durations and jitter use monotonic raw time.
