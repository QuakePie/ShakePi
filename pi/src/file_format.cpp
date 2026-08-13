#include "shakepie/file_format.hpp"

#include <algorithm>
#include <array>
#include <type_traits>

namespace shakepie::file_format {
namespace {

constexpr std::array<std::byte, 4> kHeaderMagic{
    std::byte{'S'}, std::byte{'P'}, std::byte{'F'}, std::byte{'H'}};
constexpr std::array<std::byte, 4> kFooterMagic{
    std::byte{'S'}, std::byte{'P'}, std::byte{'F'}, std::byte{'T'}};
constexpr std::array<std::uint16_t, 7> kValidGains{1, 2, 4, 8, 16, 32, 64};
constexpr std::uint32_t kKnownFooterFlags = 1U;

bool valid_gain(std::uint16_t gain) noexcept {
    return std::find(kValidGains.begin(), kValidGains.end(), gain) != kValidGains.end();
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

template <std::size_t Size>
bool magic_matches(std::span<const std::byte> input, const std::array<std::byte, Size>& magic) {
    return input.size() >= Size && std::equal(magic.begin(), magic.end(), input.begin());
}

}  // namespace

bool encode_header(const FileHeader& header, std::span<std::byte, kFileHeaderSize> output) noexcept {
    if (header.station_id.empty() || header.station_id.size() > 31U || header.samples_per_block == 0U ||
        header.samples_per_block > protocol::kMaxSamplesPerBlock || !valid_gain(header.pga_gain) ||
        header.adc_data_rate_millihz == 0U || header.reference_voltage_uv == 0U ||
        header.file_flags != 0U ||
        !std::all_of(header.station_id.begin(), header.station_id.end(), [](unsigned char character) {
            return character >= 0x20U && character <= 0x7eU;
        })) return false;
    std::fill(output.begin(), output.end(), std::byte{0});
    std::copy(kHeaderMagic.begin(), kHeaderMagic.end(), output.begin());
    put_le<std::uint16_t>(output, 4, kFileVersion);
    put_le<std::uint16_t>(output, 6, static_cast<std::uint16_t>(kFileHeaderSize));
    put_le<std::uint16_t>(output, 8, protocol::kProtocolVersion);
    put_le<std::uint16_t>(output, 10, static_cast<std::uint16_t>(protocol::kBlockHeaderSize));
    put_le<std::uint16_t>(output, 12, static_cast<std::uint16_t>(protocol::kSampleGroupSize));
    output[14] = std::byte{3};
    output[15] = static_cast<std::byte>(protocol::kChannelOrderNez);
    put_le<std::uint16_t>(output, 16, header.pga_gain);
    put_le<std::uint16_t>(output, 18, header.file_flags);
    put_le<std::uint32_t>(output, 20, header.adc_data_rate_millihz);
    put_le<std::uint32_t>(output, 24, header.reference_voltage_uv);
    put_le<std::uint32_t>(output, 28, header.samples_per_block);
    put_le<std::uint64_t>(output, 32, header.created_utc_ns);
    put_le<std::uint64_t>(output, 40, header.created_monotonic_ns);
    put_le<std::uint64_t>(output, 48, header.first_block_sequence);
    put_le<std::uint64_t>(output, 56, header.first_sample_sequence);
    for (std::size_t index = 0; index < header.station_id.size(); ++index) {
        output[64 + index] = static_cast<std::byte>(static_cast<unsigned char>(header.station_id[index]));
    }
    put_le<std::uint32_t>(output, 124, protocol::crc32c(output.first(124)));
    return true;
}

FileDecodeError decode_header(std::span<const std::byte> encoded, FileHeader& header) noexcept {
    if (encoded.size() != kFileHeaderSize) return FileDecodeError::wrong_size;
    if (!magic_matches(encoded, kHeaderMagic)) return FileDecodeError::bad_magic;
    if (get_le<std::uint16_t>(encoded, 4) != kFileVersion) return FileDecodeError::unsupported_version;
    if (get_le<std::uint16_t>(encoded, 6) != kFileHeaderSize ||
        get_le<std::uint16_t>(encoded, 8) != protocol::kProtocolVersion ||
        get_le<std::uint16_t>(encoded, 10) != protocol::kBlockHeaderSize ||
        get_le<std::uint16_t>(encoded, 12) != protocol::kSampleGroupSize ||
        std::to_integer<unsigned>(encoded[14]) != 3U ||
        std::to_integer<unsigned>(encoded[15]) != protocol::kChannelOrderNez) {
        return FileDecodeError::invalid_layout;
    }
    if (get_le<std::uint32_t>(encoded, 124) != protocol::crc32c(encoded.first(124))) {
        return FileDecodeError::invalid_crc;
    }
    std::size_t station_length = 0;
    while (station_length < 32U && encoded[64 + station_length] != std::byte{0}) {
        const auto character = std::to_integer<unsigned>(encoded[64 + station_length]);
        if (character < 0x20U || character > 0x7eU) return FileDecodeError::invalid_text;
        ++station_length;
    }
    if (station_length == 0U || station_length == 32U) return FileDecodeError::invalid_text;
    if (!std::all_of(encoded.begin() + static_cast<std::ptrdiff_t>(64U + station_length),
                     encoded.begin() + 124,
                     [](std::byte value) { return value == std::byte{0}; })) {
        return FileDecodeError::invalid_layout;
    }
    header.pga_gain = get_le<std::uint16_t>(encoded, 16);
    header.file_flags = get_le<std::uint16_t>(encoded, 18);
    header.adc_data_rate_millihz = get_le<std::uint32_t>(encoded, 20);
    header.reference_voltage_uv = get_le<std::uint32_t>(encoded, 24);
    header.samples_per_block = get_le<std::uint32_t>(encoded, 28);
    if (header.samples_per_block == 0U || header.samples_per_block > protocol::kMaxSamplesPerBlock ||
        !valid_gain(header.pga_gain) || header.file_flags != 0U ||
        header.adc_data_rate_millihz == 0U ||
        header.reference_voltage_uv == 0U) {
        return FileDecodeError::invalid_layout;
    }
    header.created_utc_ns = get_le<std::uint64_t>(encoded, 32);
    header.created_monotonic_ns = get_le<std::uint64_t>(encoded, 40);
    header.first_block_sequence = get_le<std::uint64_t>(encoded, 48);
    header.first_sample_sequence = get_le<std::uint64_t>(encoded, 56);
    header.station_id.clear();
    header.station_id.reserve(station_length);
    for (std::size_t index = 0; index < station_length; ++index) {
        header.station_id.push_back(static_cast<char>(std::to_integer<unsigned>(encoded[64 + index])));
    }
    return FileDecodeError::none;
}

bool encode_footer(const FileFooter& footer, std::span<std::byte, kFileFooterSize> output) noexcept {
    if (footer.block_count == 0U || footer.sample_count == 0U ||
        footer.last_block_sequence < footer.first_block_sequence ||
        footer.last_sample_sequence < footer.first_sample_sequence ||
        footer.last_block_sequence - footer.first_block_sequence != footer.block_count - 1U ||
        footer.last_sample_sequence - footer.first_sample_sequence < footer.sample_count - 1U ||
        (footer.footer_flags & ~kKnownFooterFlags) != 0U) {
        return false;
    }
    std::fill(output.begin(), output.end(), std::byte{0});
    std::copy(kFooterMagic.begin(), kFooterMagic.end(), output.begin());
    put_le<std::uint16_t>(output, 4, kFileVersion);
    put_le<std::uint16_t>(output, 6, static_cast<std::uint16_t>(kFileFooterSize));
    put_le<std::uint64_t>(output, 8, footer.block_count);
    put_le<std::uint64_t>(output, 16, footer.sample_count);
    put_le<std::uint64_t>(output, 24, footer.first_block_sequence);
    put_le<std::uint64_t>(output, 32, footer.last_block_sequence);
    put_le<std::uint64_t>(output, 40, footer.first_sample_sequence);
    put_le<std::uint64_t>(output, 48, footer.last_sample_sequence);
    put_le<std::uint32_t>(output, 56, footer.footer_flags);
    put_le<std::uint32_t>(output, 60, protocol::crc32c(output.first(60)));
    return true;
}

FileDecodeError decode_footer(std::span<const std::byte> encoded, FileFooter& footer) noexcept {
    if (encoded.size() != kFileFooterSize) return FileDecodeError::wrong_size;
    if (!magic_matches(encoded, kFooterMagic)) return FileDecodeError::bad_magic;
    if (get_le<std::uint16_t>(encoded, 4) != kFileVersion) return FileDecodeError::unsupported_version;
    if (get_le<std::uint16_t>(encoded, 6) != kFileFooterSize) return FileDecodeError::invalid_layout;
    if (get_le<std::uint32_t>(encoded, 60) != protocol::crc32c(encoded.first(60))) {
        return FileDecodeError::invalid_crc;
    }
    footer.block_count = get_le<std::uint64_t>(encoded, 8);
    footer.sample_count = get_le<std::uint64_t>(encoded, 16);
    footer.first_block_sequence = get_le<std::uint64_t>(encoded, 24);
    footer.last_block_sequence = get_le<std::uint64_t>(encoded, 32);
    footer.first_sample_sequence = get_le<std::uint64_t>(encoded, 40);
    footer.last_sample_sequence = get_le<std::uint64_t>(encoded, 48);
    footer.footer_flags = get_le<std::uint32_t>(encoded, 56);
    if (footer.block_count == 0U || footer.sample_count == 0U ||
        footer.last_block_sequence < footer.first_block_sequence ||
        footer.last_sample_sequence < footer.first_sample_sequence ||
        footer.last_block_sequence - footer.first_block_sequence != footer.block_count - 1U ||
        footer.last_sample_sequence - footer.first_sample_sequence < footer.sample_count - 1U ||
        (footer.footer_flags & ~kKnownFooterFlags) != 0U) {
        return FileDecodeError::invalid_layout;
    }
    return FileDecodeError::none;
}

std::string_view decode_error_message(FileDecodeError error) noexcept {
    switch (error) {
        case FileDecodeError::none: return "no error";
        case FileDecodeError::wrong_size: return "record has the wrong size";
        case FileDecodeError::bad_magic: return "record magic is invalid";
        case FileDecodeError::unsupported_version: return "file version is unsupported";
        case FileDecodeError::invalid_layout: return "file layout metadata is invalid";
        case FileDecodeError::invalid_crc: return "file record CRC is invalid";
        case FileDecodeError::invalid_text: return "file text metadata is invalid";
    }
    return "unknown file decode error";
}

}  // namespace shakepie::file_format
