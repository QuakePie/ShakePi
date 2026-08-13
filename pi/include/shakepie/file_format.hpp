#pragma once

#include "shakepie/protocol.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>

namespace shakepie::file_format {

inline constexpr std::uint16_t kFileVersion = 1;
inline constexpr std::size_t kFileHeaderSize = 128;
inline constexpr std::size_t kFileFooterSize = 64;

struct FileHeader {
    std::uint16_t pga_gain{};
    std::uint16_t file_flags{};
    std::uint32_t adc_data_rate_millihz{};
    std::uint32_t reference_voltage_uv{};
    std::uint32_t samples_per_block{};
    std::uint64_t created_utc_ns{};
    std::uint64_t created_monotonic_ns{};
    std::uint64_t first_block_sequence{};
    std::uint64_t first_sample_sequence{};
    std::string station_id{};

    friend bool operator==(const FileHeader&, const FileHeader&) = default;
};

struct FileFooter {
    std::uint64_t block_count{};
    std::uint64_t sample_count{};
    std::uint64_t first_block_sequence{};
    std::uint64_t last_block_sequence{};
    std::uint64_t first_sample_sequence{};
    std::uint64_t last_sample_sequence{};
    std::uint32_t footer_flags{};

    friend constexpr bool operator==(const FileFooter&, const FileFooter&) = default;
};

enum class FileDecodeError {
    none,
    wrong_size,
    bad_magic,
    unsupported_version,
    invalid_layout,
    invalid_crc,
    invalid_text,
};

[[nodiscard]] bool encode_header(const FileHeader& header,
                                 std::span<std::byte, kFileHeaderSize> output) noexcept;
[[nodiscard]] FileDecodeError decode_header(std::span<const std::byte> encoded,
                                            FileHeader& header) noexcept;
[[nodiscard]] bool encode_footer(const FileFooter& footer,
                                 std::span<std::byte, kFileFooterSize> output) noexcept;
[[nodiscard]] FileDecodeError decode_footer(std::span<const std::byte> encoded,
                                            FileFooter& footer) noexcept;
[[nodiscard]] std::string_view decode_error_message(FileDecodeError error) noexcept;

}  // namespace shakepie::file_format
