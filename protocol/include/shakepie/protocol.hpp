#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace shakepie::protocol {

inline constexpr std::uint16_t kProtocolVersion = 1;
inline constexpr std::size_t kBlockHeaderSize = 64;
inline constexpr std::size_t kSampleGroupSize = 16;
inline constexpr std::size_t kBlockCrcSize = 4;
inline constexpr std::uint32_t kDefaultSamplesPerBlock = 64;
inline constexpr std::uint32_t kMaxSamplesPerBlock = 256;
inline constexpr std::size_t kMaxEncodedBlockSize =
    kBlockHeaderSize + (kMaxSamplesPerBlock * kSampleGroupSize) + kBlockCrcSize;
inline constexpr std::uint8_t kChannelOrderNez = 0;

enum class StatusFlag : std::uint32_t {
    none = 0,
    adc_timeout_seen = 1U << 0U,
    adc_fault_seen = 1U << 1U,
    simulated = 1U << 2U,
    clipping_seen = 1U << 3U,
    storage_pressure = 1U << 4U,
};

enum class TimingFlag : std::uint32_t {
    none = 0,
    utc_clock_step = 1U << 0U,
    acquisition_late = 1U << 1U,
    timestamp_saturated = 1U << 2U,
};

struct SampleGroup {
    std::int32_t north_count{};
    std::int32_t east_count{};
    std::int32_t vertical_count{};
    std::uint32_t nanoseconds_from_block_start{};

    friend constexpr bool operator==(const SampleGroup&, const SampleGroup&) = default;
};

struct BlockHeader {
    std::uint32_t status_flags{};
    std::uint64_t block_sequence{};
    std::uint64_t first_sample_sequence{};
    std::uint64_t utc_start_nanoseconds{};
    std::uint64_t monotonic_start_nanoseconds{};
    std::uint32_t sample_count{};
    std::uint32_t timing_flags{};
    std::uint32_t nominal_group_rate_millihz{};
    std::uint8_t channel_order{kChannelOrderNez};

    friend constexpr bool operator==(const BlockHeader&, const BlockHeader&) = default;
};

struct SampleBlock {
    BlockHeader header{};
    std::array<SampleGroup, kMaxSamplesPerBlock> samples{};
};

enum class DecodeError {
    none,
    too_short,
    bad_magic,
    unsupported_version,
    invalid_header_length,
    invalid_payload_length,
    impossible_sample_count,
    invalid_sample_layout,
    invalid_channel_order,
    impossible_sequence,
    invalid_sample_value,
    invalid_sample_timing,
    invalid_crc,
};

[[nodiscard]] constexpr std::size_t encoded_block_size(std::uint32_t sample_count) noexcept {
    return kBlockHeaderSize + (static_cast<std::size_t>(sample_count) * kSampleGroupSize) + kBlockCrcSize;
}

[[nodiscard]] std::uint32_t crc32c(std::span<const std::byte> data) noexcept;

// Returns zero when the destination is too small or the block is invalid.
[[nodiscard]] std::size_t encode_block(const SampleBlock& block, std::span<std::byte> destination) noexcept;

[[nodiscard]] DecodeError decode_block(std::span<const std::byte> encoded, SampleBlock& destination) noexcept;

[[nodiscard]] std::string_view decode_error_message(DecodeError error) noexcept;

}  // namespace shakepie::protocol
