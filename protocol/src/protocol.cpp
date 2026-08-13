#include "shakepie/protocol.hpp"

#include <algorithm>
#include <array>
#include <limits>
#include <type_traits>

namespace shakepie::protocol {
namespace {

constexpr std::array<std::byte, 4> kMagic{
    std::byte{'S'}, std::byte{'P'}, std::byte{'B'}, std::byte{'K'}};

bool valid_count(std::int32_t value) noexcept {
    return value >= -8'388'608 && value <= 8'388'607;
}

template <typename T>
void put_le(std::span<std::byte> output, std::size_t offset, T value) noexcept {
    using U = std::make_unsigned_t<T>;
    U bits = static_cast<U>(value);
    for (std::size_t index = 0; index < sizeof(T); ++index) {
        output[offset + index] = static_cast<std::byte>((bits >> (index * 8U)) & static_cast<U>(0xffU));
    }
}

template <typename T>
T get_le(std::span<const std::byte> input, std::size_t offset) noexcept {
    using U = std::make_unsigned_t<T>;
    U bits = 0;
    for (std::size_t index = 0; index < sizeof(T); ++index) {
        const auto shifted = static_cast<std::uintmax_t>(
                                 std::to_integer<unsigned int>(input[offset + index]))
                             << (index * 8U);
        bits = static_cast<U>(static_cast<std::uintmax_t>(bits) | shifted);
    }
    return static_cast<T>(bits);
}

}  // namespace

std::uint32_t crc32c(std::span<const std::byte> data) noexcept {
    std::uint32_t crc = 0xffffffffU;
    for (const std::byte value : data) {
        crc ^= std::to_integer<std::uint8_t>(value);
        for (unsigned bit = 0; bit < 8U; ++bit) {
            const std::uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (0x82f63b78U & mask);
        }
    }
    return ~crc;
}

std::size_t encode_block(const SampleBlock& block, std::span<std::byte> destination) noexcept {
    const std::uint32_t count = block.header.sample_count;
    if (count == 0U || count > kMaxSamplesPerBlock || block.header.channel_order != kChannelOrderNez) {
        return 0;
    }
    if (block.header.first_sample_sequence >
            std::numeric_limits<std::uint64_t>::max() - (count - 1U) ||
        block.header.nominal_group_rate_millihz == 0U) {
        return 0;
    }
    for (std::uint32_t index = 1; index < count; ++index) {
        if (block.samples[index].nanoseconds_from_block_start <
            block.samples[index - 1U].nanoseconds_from_block_start) {
            return 0;
        }
    }
    for (std::uint32_t index = 0; index < count; ++index) {
        const auto& sample = block.samples[index];
        if (!valid_count(sample.north_count) || !valid_count(sample.east_count) ||
            !valid_count(sample.vertical_count)) {
            return 0;
        }
    }
    const std::size_t required = encoded_block_size(count);
    if (destination.size() < required) {
        return 0;
    }

    std::fill_n(destination.begin(), required, std::byte{0});
    std::copy(kMagic.begin(), kMagic.end(), destination.begin());
    put_le<std::uint16_t>(destination, 4, kProtocolVersion);
    put_le<std::uint16_t>(destination, 6, static_cast<std::uint16_t>(kBlockHeaderSize));
    put_le<std::uint32_t>(destination, 8, count * static_cast<std::uint32_t>(kSampleGroupSize));
    put_le<std::uint32_t>(destination, 12, block.header.status_flags);
    put_le<std::uint64_t>(destination, 16, block.header.block_sequence);
    put_le<std::uint64_t>(destination, 24, block.header.first_sample_sequence);
    put_le<std::uint64_t>(destination, 32, block.header.utc_start_nanoseconds);
    put_le<std::uint64_t>(destination, 40, block.header.monotonic_start_nanoseconds);
    put_le<std::uint32_t>(destination, 48, count);
    put_le<std::uint32_t>(destination, 52, block.header.timing_flags);
    put_le<std::uint32_t>(destination, 56, block.header.nominal_group_rate_millihz);
    put_le<std::uint16_t>(destination, 60, static_cast<std::uint16_t>(kSampleGroupSize));
    destination[62] = static_cast<std::byte>(3U);
    destination[63] = static_cast<std::byte>(block.header.channel_order);

    std::size_t offset = kBlockHeaderSize;
    for (std::uint32_t index = 0; index < count; ++index) {
        const auto& sample = block.samples[index];
        put_le<std::int32_t>(destination, offset, sample.north_count);
        put_le<std::int32_t>(destination, offset + 4, sample.east_count);
        put_le<std::int32_t>(destination, offset + 8, sample.vertical_count);
        put_le<std::uint32_t>(destination, offset + 12, sample.nanoseconds_from_block_start);
        offset += kSampleGroupSize;
    }
    put_le<std::uint32_t>(destination, required - kBlockCrcSize,
                          crc32c(destination.first(required - kBlockCrcSize)));
    return required;
}

DecodeError decode_block(std::span<const std::byte> encoded, SampleBlock& destination) noexcept {
    if (encoded.size() < kBlockHeaderSize + kBlockCrcSize) {
        return DecodeError::too_short;
    }
    if (!std::equal(kMagic.begin(), kMagic.end(), encoded.begin())) {
        return DecodeError::bad_magic;
    }
    if (get_le<std::uint16_t>(encoded, 4) != kProtocolVersion) {
        return DecodeError::unsupported_version;
    }
    if (get_le<std::uint16_t>(encoded, 6) != kBlockHeaderSize) {
        return DecodeError::invalid_header_length;
    }
    const std::uint32_t payload_length = get_le<std::uint32_t>(encoded, 8);
    const std::uint32_t count = get_le<std::uint32_t>(encoded, 48);
    if (count == 0U || count > kMaxSamplesPerBlock) {
        return DecodeError::impossible_sample_count;
    }
    if (payload_length != count * kSampleGroupSize) {
        return DecodeError::invalid_payload_length;
    }
    const auto first_sample_sequence = get_le<std::uint64_t>(encoded, 24);
    if (first_sample_sequence > std::numeric_limits<std::uint64_t>::max() - (count - 1U)) {
        return DecodeError::impossible_sequence;
    }
    if (get_le<std::uint32_t>(encoded, 56) == 0U) {
        return DecodeError::invalid_sample_timing;
    }
    if (get_le<std::uint16_t>(encoded, 60) != kSampleGroupSize ||
        std::to_integer<std::uint8_t>(encoded[62]) != 3U) {
        return DecodeError::invalid_sample_layout;
    }
    if (std::to_integer<std::uint8_t>(encoded[63]) != kChannelOrderNez) {
        return DecodeError::invalid_channel_order;
    }
    const std::size_t expected_size = encoded_block_size(count);
    if (encoded.size() != expected_size) {
        return DecodeError::invalid_payload_length;
    }
    const std::uint32_t expected_crc = get_le<std::uint32_t>(encoded, expected_size - kBlockCrcSize);
    if (crc32c(encoded.first(expected_size - kBlockCrcSize)) != expected_crc) {
        return DecodeError::invalid_crc;
    }

    destination.header.status_flags = get_le<std::uint32_t>(encoded, 12);
    destination.header.block_sequence = get_le<std::uint64_t>(encoded, 16);
    destination.header.first_sample_sequence = first_sample_sequence;
    destination.header.utc_start_nanoseconds = get_le<std::uint64_t>(encoded, 32);
    destination.header.monotonic_start_nanoseconds = get_le<std::uint64_t>(encoded, 40);
    destination.header.sample_count = count;
    destination.header.timing_flags = get_le<std::uint32_t>(encoded, 52);
    destination.header.nominal_group_rate_millihz = get_le<std::uint32_t>(encoded, 56);
    destination.header.channel_order = std::to_integer<std::uint8_t>(encoded[63]);

    std::size_t offset = kBlockHeaderSize;
    for (std::uint32_t index = 0; index < count; ++index) {
        destination.samples[index].north_count = get_le<std::int32_t>(encoded, offset);
        destination.samples[index].east_count = get_le<std::int32_t>(encoded, offset + 4);
        destination.samples[index].vertical_count = get_le<std::int32_t>(encoded, offset + 8);
        if (!valid_count(destination.samples[index].north_count) ||
            !valid_count(destination.samples[index].east_count) ||
            !valid_count(destination.samples[index].vertical_count)) {
            return DecodeError::invalid_sample_value;
        }
        destination.samples[index].nanoseconds_from_block_start = get_le<std::uint32_t>(encoded, offset + 12);
        if (index != 0U && destination.samples[index].nanoseconds_from_block_start <
                               destination.samples[index - 1U].nanoseconds_from_block_start) {
            return DecodeError::invalid_sample_timing;
        }
        offset += kSampleGroupSize;
    }
    return DecodeError::none;
}

std::string_view decode_error_message(DecodeError error) noexcept {
    switch (error) {
        case DecodeError::none: return "no error";
        case DecodeError::too_short: return "block is too short";
        case DecodeError::bad_magic: return "block magic is invalid";
        case DecodeError::unsupported_version: return "protocol version is unsupported";
        case DecodeError::invalid_header_length: return "header length is invalid";
        case DecodeError::invalid_payload_length: return "payload length is invalid";
        case DecodeError::impossible_sample_count: return "sample count is impossible";
        case DecodeError::invalid_sample_layout: return "sample layout is invalid";
        case DecodeError::invalid_channel_order: return "channel order is unsupported";
        case DecodeError::impossible_sequence: return "sample sequence range overflows";
        case DecodeError::invalid_sample_value: return "sample count exceeds the signed 24-bit range";
        case DecodeError::invalid_sample_timing: return "sample timing metadata is invalid";
        case DecodeError::invalid_crc: return "CRC32C does not match";
    }
    return "unknown decode error";
}

}  // namespace shakepie::protocol
