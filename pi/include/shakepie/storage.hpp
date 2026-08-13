#pragma once

#include "shakepie/clocks.hpp"
#include "shakepie/config.hpp"
#include "shakepie/file_format.hpp"
#include "shakepie/health.hpp"
#include "shakepie/queues.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <span>
#include <string>
#include <vector>

#if defined(__linux__)
#include <sys/types.h>
#else
using ssize_t = std::ptrdiff_t;
#endif

namespace shakepie {

using WriteOperation = std::function<ssize_t(const std::byte*, std::size_t)>;

[[nodiscard]] bool write_all(std::span<const std::byte> data,
                             const WriteOperation& operation,
                             int& error_number) noexcept;

struct MountCheck {
    bool ok{};
    std::string error{};
};

[[nodiscard]] MountCheck verify_hdd_mount(const std::filesystem::path& mount,
                                          const std::filesystem::path& data_directory);

struct RecoveryReport {
    std::size_t files_seen{};
    std::size_t files_recovered{};
    std::size_t files_finalized{};
    std::size_t files_quarantined{};
    std::uint64_t bytes_truncated{};
    std::vector<std::string> errors{};

    [[nodiscard]] bool ok() const noexcept { return errors.empty(); }
};

[[nodiscard]] RecoveryReport recover_partial_files(const StorageConfig& config);

class StorageWriter {
public:
    StorageWriter(StorageConfig storage_config,
                  AdcConfig adc_config,
                  AcquisitionConfig acquisition_config,
                  SpscRing<protocol::SampleBlock>& queue,
                  HealthCounters& health,
                  IClock& clock,
                  std::atomic<bool>& stop_requested,
                  std::atomic<bool>& acquisition_done);
    ~StorageWriter();

    StorageWriter(const StorageWriter&) = delete;
    StorageWriter& operator=(const StorageWriter&) = delete;

    void run() noexcept;
    [[nodiscard]] const std::string& last_error() const noexcept { return last_error_; }

private:
    [[nodiscard]] bool open_file(const protocol::SampleBlock& first) noexcept;
    [[nodiscard]] bool append_block(const protocol::SampleBlock& block) noexcept;
    [[nodiscard]] bool flush_aggregate() noexcept;
    [[nodiscard]] bool synchronize() noexcept;
    [[nodiscard]] bool finalize_file(bool recovered = false) noexcept;
    void fail(FatalCode code, std::string message) noexcept;
    void update_disk_space() noexcept;

    StorageConfig storage_config_;
    AdcConfig adc_config_;
    AcquisitionConfig acquisition_config_;
    SpscRing<protocol::SampleBlock>& queue_;
    HealthCounters& health_;
    IClock& clock_;
    std::atomic<bool>& stop_requested_;
    std::atomic<bool>& acquisition_done_;
    int directory_descriptor_{-1};
    int descriptor_{-1};
    std::filesystem::path partial_path_{};
    std::filesystem::path final_path_{};
    std::vector<std::byte> aggregate_{};
    std::uint64_t aggregate_blocks_{0};
    file_format::FileFooter footer_{};
    std::uint64_t rotation_bucket_{0};
    std::uint64_t last_sync_monotonic_ns_{0};
    std::uint64_t logical_size_{0};
    bool failed_{false};
    std::string last_error_{};
};

}  // namespace shakepie
