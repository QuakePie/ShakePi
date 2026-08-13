#include "test_harness.hpp"

#include "shakepie/clocks.hpp"
#include "shakepie/file_format.hpp"
#include "shakepie/storage.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <span>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <unistd.h>

namespace {

shakepie::protocol::SampleBlock make_block(std::uint64_t block_sequence,
                                          std::uint64_t sample_sequence,
                                          std::uint64_t utc_ns) {
    shakepie::protocol::SampleBlock block{};
    block.header.block_sequence = block_sequence;
    block.header.first_sample_sequence = sample_sequence;
    block.header.utc_start_nanoseconds = utc_ns;
    block.header.monotonic_start_nanoseconds = utc_ns;
    block.header.sample_count = 2;
    block.header.nominal_group_rate_millihz = 750'000;
    block.samples[0] = {1, 2, 3, 0};
    block.samples[1] = {4, 5, 6, 1'000};
    return block;
}

std::filesystem::path temporary_directory() {
    auto path = std::filesystem::temp_directory_path() /
                ("shakepie-test-" + std::to_string(static_cast<unsigned long long>(::getpid())));
    std::filesystem::remove_all(path);
    std::filesystem::create_directories(path);
    return path;
}

std::filesystem::path containing_mount(const std::filesystem::path& path) {
    std::error_code error;
    auto current = std::filesystem::canonical(path, error);
    if (error) return {};
    struct stat current_information {};
    if (::stat(current.c_str(), &current_information) != 0) return {};
    while (current != current.root_path()) {
        const auto parent = current.parent_path();
        struct stat parent_information {};
        if (::stat(parent.c_str(), &parent_information) != 0) return {};
        if (parent_information.st_dev != current_information.st_dev) return current;
        current = parent;
        current_information = parent_information;
    }
    return current;
}

void append_bytes(std::ofstream& output, std::span<const std::byte> bytes) {
    output.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
}

}  // namespace

int main() {
    std::vector<std::byte> source(17, std::byte{0x5a});
    std::vector<std::byte> destination;
    unsigned calls = 0;
    int io_error = 0;
    CHECK(shakepie::write_all(source, [&](const std::byte* data, std::size_t size) -> ssize_t {
        ++calls;
        if (calls == 1U) {
            errno = EINTR;
            return -1;
        }
        const auto count = std::min<std::size_t>(3, size);
        destination.insert(destination.end(), data, data + count);
        return static_cast<ssize_t>(count);
    }, io_error));
    CHECK(destination == source);
    CHECK(calls > 2U);
    CHECK(!shakepie::write_all(source, [](const std::byte*, std::size_t) -> ssize_t {
        errno = ENOSPC;
        return -1;
    }, io_error));
    CHECK(io_error == ENOSPC);
    CHECK(!shakepie::write_all(source, [](const std::byte*, std::size_t) -> ssize_t {
        errno = EIO;
        return -1;
    }, io_error));
    CHECK(io_error == EIO);

    const auto directory = temporary_directory();
    const auto directory_mount = containing_mount(directory);
    CHECK(!directory_mount.empty());
    if (directory_mount.empty()) return 1;
    CHECK(shakepie::verify_hdd_mount(directory_mount, directory).ok);
    const auto ordinary_directory = directory / "not-a-mount";
    std::filesystem::create_directories(ordinary_directory / "recordings");
    CHECK(!shakepie::verify_hdd_mount(ordinary_directory,
                                      ordinary_directory / "recordings").ok);
    CHECK(!shakepie::verify_hdd_mount(directory / "missing",
                                      directory / "missing" / "recordings").ok);
    CHECK(!shakepie::verify_hdd_mount("/", directory / "missing-data-directory").ok);

    shakepie::file_format::FileHeader header{};
    header.pga_gain = 64;
    header.adc_data_rate_millihz = 7'500'000;
    header.reference_voltage_uv = 2'500'000;
    header.samples_per_block = 64;
    header.created_utc_ns = 1;
    header.created_monotonic_ns = 2;
    header.first_block_sequence = 3;
    header.first_sample_sequence = 4;
    header.station_id = "TEST";
    std::array<std::byte, shakepie::file_format::kFileHeaderSize> header_bytes{};
    CHECK(shakepie::file_format::encode_header(header, header_bytes));
    shakepie::file_format::FileHeader decoded_header{};
    CHECK(shakepie::file_format::decode_header(header_bytes, decoded_header) ==
          shakepie::file_format::FileDecodeError::none);
    CHECK(decoded_header == header);
    auto invalid_header = header;
    invalid_header.pga_gain = 3;
    CHECK(!shakepie::file_format::encode_header(invalid_header, header_bytes));
    invalid_header = header;
    invalid_header.file_flags = 1;
    CHECK(!shakepie::file_format::encode_header(invalid_header, header_bytes));
    CHECK(shakepie::file_format::encode_header(header, header_bytes));
    header_bytes[100] = std::byte{1};
    const auto header_crc = shakepie::protocol::crc32c(std::span(header_bytes).first(124));
    for (unsigned index = 0; index < 4U; ++index) {
        header_bytes[124U + index] =
            static_cast<std::byte>((header_crc >> (index * 8U)) & 0xffU);
    }
    CHECK(shakepie::file_format::decode_header(header_bytes, decoded_header) ==
          shakepie::file_format::FileDecodeError::invalid_layout);
    CHECK(shakepie::file_format::encode_header(header, header_bytes));

    const auto partial = directory / "interrupted.partial";
    auto first = make_block(0, 0, 60'000'000'000ULL);
    auto second = make_block(1, 2, 61'000'000'000ULL);
    std::array<std::byte, shakepie::protocol::kMaxEncodedBlockSize> encoded_first{};
    std::array<std::byte, shakepie::protocol::kMaxEncodedBlockSize> encoded_second{};
    const auto first_size = shakepie::protocol::encode_block(first, encoded_first);
    const auto second_size = shakepie::protocol::encode_block(second, encoded_second);
    auto recovery_header = header;
    recovery_header.first_block_sequence = 0;
    recovery_header.first_sample_sequence = 0;
    CHECK(shakepie::file_format::encode_header(recovery_header, header_bytes));
    {
        std::ofstream output(partial, std::ios::binary | std::ios::trunc);
        append_bytes(output, header_bytes);
        std::array<std::byte, 4> length{};
        for (unsigned index = 0; index < 4U; ++index) {
            length[index] = static_cast<std::byte>((first_size >> (index * 8U)) & 0xffU);
        }
        append_bytes(output, length);
        append_bytes(output, std::span(encoded_first).first(first_size));
        for (unsigned index = 0; index < 4U; ++index) {
            length[index] = static_cast<std::byte>((second_size >> (index * 8U)) & 0xffU);
        }
        append_bytes(output, length);
        append_bytes(output, std::span(encoded_second).first(second_size / 2U));
    }
    {
        std::ofstream output(directory / "empty.partial", std::ios::binary | std::ios::trunc);
        append_bytes(output, header_bytes);
        // Model an interrupted preallocation: the logical file contains only
        // a valid header followed by unwritten zero-filled space.
        std::array<std::byte, 4'096> zeros{};
        append_bytes(output, zeros);
    }
    {
        std::ofstream(directory / "zero.partial", std::ios::binary | std::ios::trunc);
    }
    {
        std::ofstream output(directory / "complete.partial", std::ios::binary | std::ios::trunc);
        append_bytes(output, header_bytes);
        std::array<std::byte, 4> length{};
        for (unsigned index = 0; index < 4U; ++index) {
            length[index] = static_cast<std::byte>((first_size >> (index * 8U)) & 0xffU);
        }
        append_bytes(output, length);
        append_bytes(output, std::span(encoded_first).first(first_size));
        shakepie::file_format::FileFooter footer{};
        footer.block_count = 1;
        footer.sample_count = first.header.sample_count;
        footer.first_block_sequence = first.header.block_sequence;
        footer.last_block_sequence = first.header.block_sequence;
        footer.first_sample_sequence = first.header.first_sample_sequence;
        footer.last_sample_sequence = first.header.first_sample_sequence + first.header.sample_count - 1U;
        std::array<std::byte, shakepie::file_format::kFileFooterSize> footer_bytes{};
        auto invalid_footer = footer;
        invalid_footer.footer_flags = 2U;
        CHECK(!shakepie::file_format::encode_footer(invalid_footer, footer_bytes));
        CHECK(shakepie::file_format::encode_footer(footer, footer_bytes));
        append_bytes(output, footer_bytes);
    }
    shakepie::StorageConfig recovery_config{};
    recovery_config.hdd_mount = directory_mount;
    recovery_config.data_directory = directory;
    const auto recovery = shakepie::recover_partial_files(recovery_config);
    CHECK(recovery.ok());
    CHECK(recovery.files_recovered == 1U);
    CHECK(recovery.files_finalized == 1U);
    CHECK(recovery.files_quarantined == 2U);
    CHECK(recovery.bytes_truncated == 4U + second_size / 2U);
    CHECK(std::filesystem::exists(directory / "interrupted.recovered.spb"));
    CHECK(std::filesystem::exists(directory / "complete.spb"));
    CHECK(std::filesystem::exists(directory / "empty.partial.empty"));
    CHECK(std::filesystem::exists(directory / "zero.partial.empty"));

    const auto recovered_size = std::filesystem::file_size(directory / "interrupted.recovered.spb");
    {
        std::ofstream output(partial, std::ios::binary | std::ios::trunc);
        append_bytes(output, header_bytes);
        std::array<std::byte, 4> length{};
        for (unsigned index = 0; index < 4U; ++index) {
            length[index] = static_cast<std::byte>((first_size >> (index * 8U)) & 0xffU);
        }
        append_bytes(output, length);
        append_bytes(output, std::span(encoded_first).first(first_size));
    }
    const auto collision = shakepie::recover_partial_files(recovery_config);
    CHECK(!collision.ok());
    CHECK(std::filesystem::exists(partial));
    CHECK(std::filesystem::file_size(directory / "interrupted.recovered.spb") == recovered_size);
    const auto repeated_collision = shakepie::recover_partial_files(recovery_config);
    CHECK(!repeated_collision.ok());
    CHECK(std::filesystem::exists(partial));
    CHECK(std::filesystem::file_size(directory / "interrupted.recovered.spb") == recovered_size);

    shakepie::SpscRing<shakepie::protocol::SampleBlock> queue(4);
    CHECK(queue.try_push(make_block(10, 100, 60'000'000'000ULL)));
    CHECK(queue.try_push(make_block(11, 102, 120'000'000'000ULL)));
    std::atomic<bool> stop{false};
    std::atomic<bool> done{true};
    shakepie::HealthCounters health;
    shakepie::SystemClock clock;
    shakepie::StorageConfig storage_config{};
    storage_config.hdd_mount = directory_mount;
    storage_config.data_directory = directory / "rotation";
    storage_config.rotation_minutes = 1;
    storage_config.sync_interval_seconds = 300;
    storage_config.aggregate_bytes = 64U * 1024U;
    storage_config.station_id = "TEST";
    std::filesystem::create_directories(storage_config.data_directory);
    shakepie::AdcConfig adc_config{};
    shakepie::AcquisitionConfig acquisition_config{};
    acquisition_config.samples_per_block = 2;
    shakepie::StorageWriter writer(storage_config, adc_config, acquisition_config, queue, health,
                                   clock, stop, done);
    writer.run();
    CHECK(writer.last_error().empty());
    std::size_t completed = 0;
    for (const auto& entry : std::filesystem::directory_iterator(storage_config.data_directory)) {
        if (entry.path().extension() == ".spb") ++completed;
    }
    CHECK(completed == 2U);
    CHECK(health.storage_blocks.load() == 2U);
    std::filesystem::remove_all(directory);
    return shakepie_test_failures == 0 ? 0 : 1;
}
