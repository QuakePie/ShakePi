"""Generate the canonical v1 binary block used by C++ and Python tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / "laptop" / "src"))
    from shakepie_client.protocol import SAMPLE_DTYPE, BlockHeader, encode_block

    samples = np.array(
        [
            (-8_388_608, 0, 8_388_607, 0),
            (-123_456, 654_321, -1, 1_333_333),
            (42, -42, 2_000_000, 2_666_666),
        ],
        dtype=SAMPLE_DTYPE,
    )
    header = BlockHeader(
        status_flags=5,
        block_sequence=42,
        first_sample_sequence=1_000,
        utc_start_ns=1_700_000_000_123_456_789,
        monotonic_start_ns=9_876_543_210,
        sample_count=len(samples),
        timing_flags=2,
        nominal_group_rate_millihz=750_000,
        channel_order=0,
    )
    target = ROOT / "protocol" / "fixtures" / "golden-block-v1.spb"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_block(header, samples))


if __name__ == "__main__":
    main()
