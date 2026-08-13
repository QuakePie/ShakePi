#include "shakepie/file_format.hpp"
#include "shakepie/protocol.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <span>
#include <string>
#include <vector>

namespace {

std::uint32_t get_u32(const std::array<std::byte, 4>& input) {
    std::uint32_t value = 0;
    for (unsigned index = 0; index < 4U; ++index) {
        value |= static_cast<std::uint32_t>(std::to_integer<unsigned>(input[index])) << (index * 8U);
    }
    return value;
}

bool read_exact(std::istream& input, std::span<std::byte> output) {
    input.read(reinterpret_cast<char*>(output.data()), static_cast<std::streamsize>(output.size()));
    return input.gcount() == static_cast<std::streamsize>(output.size());
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2 && argc != 4) {
        std::cerr << "usage: shakepie-inspect RECORDING [--csv OUTPUT.csv]\n";
        return 64;
    }
    std::optional<std::filesystem::path> csv_path;
    if (argc == 4) {
        if (std::string(argv[2]) != "--csv") {
            std::cerr << "expected --csv OUTPUT.csv\n";
            return 64;
        }
        csv_path = argv[3];
    }
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) {
        std::cerr << "cannot open recording\n";
        return 66;
    }
    std::array<std::byte, shakepie::file_format::kFileHeaderSize> header_bytes{};
    if (!read_exact(input, header_bytes)) {
        std::cerr << "truncated file header\n";
        return 65;
    }
    shakepie::file_format::FileHeader header{};
    const auto header_error = shakepie::file_format::decode_header(header_bytes, header);
    if (header_error != shakepie::file_format::FileDecodeError::none) {
        std::cerr << shakepie::file_format::decode_error_message(header_error) << '\n';
        return 65;
    }
    std::ofstream csv;
    if (csv_path) {
        csv.open(*csv_path, std::ios::binary | std::ios::trunc);
        if (!csv) {
            std::cerr << "cannot open CSV output\n";
            return 73;
        }
        csv << "sample_sequence,utc_nanoseconds,north_count,east_count,vertical_count\n";
    }

    std::uint64_t blocks = 0;
    std::uint64_t samples = 0;
    std::uint64_t previous_block = 0;
    std::uint64_t previous_sample = 0;
    bool have_block = false;
    bool have_footer = false;
    while (true) {
        std::array<std::byte, 4> prefix{};
        if (!read_exact(input, prefix)) break;
        if (prefix == std::array<std::byte, 4>{std::byte{'S'}, std::byte{'P'}, std::byte{'F'}, std::byte{'T'}}) {
            std::array<std::byte, shakepie::file_format::kFileFooterSize> footer_bytes{};
            std::copy(prefix.begin(), prefix.end(), footer_bytes.begin());
            if (!read_exact(input, std::span<std::byte>(footer_bytes).subspan(4))) {
                std::cerr << "truncated footer\n";
                return 65;
            }
            shakepie::file_format::FileFooter footer{};
            if (shakepie::file_format::decode_footer(footer_bytes, footer) !=
                    shakepie::file_format::FileDecodeError::none ||
                footer.block_count != blocks || footer.sample_count != samples ||
                footer.first_block_sequence != header.first_block_sequence ||
                footer.first_sample_sequence != header.first_sample_sequence ||
                (have_block && (footer.last_block_sequence != previous_block ||
                                footer.last_sample_sequence != previous_sample))) {
                std::cerr << "invalid or inconsistent footer\n";
                return 65;
            }
            have_footer = true;
            break;
        }
        const auto length = get_u32(prefix);
        if (length < shakepie::protocol::kBlockHeaderSize + shakepie::protocol::kBlockCrcSize ||
            length > shakepie::protocol::kMaxEncodedBlockSize) {
            std::cerr << "invalid block length\n";
            return 65;
        }
        std::vector<std::byte> encoded(length);
        if (!read_exact(input, encoded)) {
            std::cerr << "truncated block\n";
            return 65;
        }
        shakepie::protocol::SampleBlock block{};
        const auto block_error = shakepie::protocol::decode_block(encoded, block);
        if (block_error != shakepie::protocol::DecodeError::none) {
            std::cerr << shakepie::protocol::decode_error_message(block_error) << '\n';
            return 65;
        }
        if (have_block && (block.header.block_sequence != previous_block + 1U ||
                           block.header.first_sample_sequence <= previous_sample)) {
            std::cerr << "out-of-order block or sample sequence\n";
            return 65;
        }
        if (!have_block &&
            (block.header.block_sequence != header.first_block_sequence ||
             block.header.first_sample_sequence != header.first_sample_sequence)) {
            std::cerr << "first block does not match file header\n";
            return 65;
        }
        for (std::uint32_t index = 0; index < block.header.sample_count; ++index) {
            const auto& sample = block.samples[index];
            if (csv) {
                const auto sequence = block.header.first_sample_sequence + index;
                const auto utc = block.header.utc_start_nanoseconds + sample.nanoseconds_from_block_start;
                csv << sequence << ',' << utc << ',' << sample.north_count << ',' << sample.east_count
                    << ',' << sample.vertical_count << '\n';
            }
        }
        ++blocks;
        samples += block.header.sample_count;
        previous_block = block.header.block_sequence;
        previous_sample = block.header.first_sample_sequence + block.header.sample_count - 1U;
        have_block = true;
    }
    if (!have_footer) {
        std::cerr << "recording has no valid footer (use recorder startup recovery for .partial files)\n";
        return 65;
    }
    if (input.peek() != std::char_traits<char>::eof()) {
        std::cerr << "trailing bytes after footer\n";
        return 65;
    }
    if (csv && !csv) {
        std::cerr << "CSV output failed\n";
        return 74;
    }
    std::cout << "station=" << header.station_id << '\n'
              << "blocks=" << blocks << '\n'
              << "samples=" << samples << '\n'
              << "pga_gain=" << header.pga_gain << '\n'
              << "adc_data_rate_millihz=" << header.adc_data_rate_millihz << '\n'
              << "reference_voltage_uv=" << header.reference_voltage_uv << '\n';
    return 0;
}
