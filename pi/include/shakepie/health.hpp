#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <string>

namespace shakepie {

enum class RecorderState : std::uint32_t { starting, running, stopping, stopped, fatal };
enum class FatalCode : std::uint32_t {
    none,
    disk_queue_full,
    adc_fault,
    storage_open,
    storage_write,
    storage_sync,
    storage_close,
    storage_rename,
    hdd_unavailable,
    stream_start,
    clock_fault,
};
enum class CalibrationPhase : std::uint32_t { idle, measuring, failed };
enum class CalibrationOutcome : std::uint32_t { none, applied, cleared, rejected, disabled };

struct HealthCounters {
    std::atomic<RecorderState> state{RecorderState::starting};
    std::atomic<FatalCode> fatal_code{FatalCode::none};
    std::atomic<std::uint64_t> acquired_groups{0};
    std::atomic<std::uint64_t> produced_blocks{0};
    std::atomic<std::uint64_t> adc_timeouts{0};
    std::atomic<std::uint64_t> adc_faults{0};
    std::atomic<std::uint64_t> missing_sample_groups{0};
    std::atomic<std::uint64_t> simulated_sequence_gaps{0};
    std::atomic<std::uint64_t> live_queue_drops{0};
    std::atomic<std::uint64_t> client_queue_drops{0};
    std::atomic<std::uint64_t> disk_queue_depth{0};
    std::atomic<std::uint64_t> disk_queue_high_water{0};
    std::atomic<std::uint64_t> storage_bytes{0};
    std::atomic<std::uint64_t> storage_blocks{0};
    std::atomic<std::uint64_t> storage_errors{0};
    std::atomic<std::uint64_t> storage_last_write_latency_us{0};
    std::atomic<std::uint64_t> storage_max_write_latency_us{0};
    std::atomic<std::uint64_t> disk_free_bytes{0};
    std::atomic<std::uint64_t> connected_clients{0};
    std::atomic<std::uint64_t> client_connections{0};
    std::atomic<std::uint64_t> client_disconnects{0};
    std::atomic<std::uint64_t> stream_errors{0};
    std::atomic<std::uint64_t> last_block_sequence{0};
    std::atomic<std::uint64_t> last_sample_sequence{0};
    std::atomic<std::int64_t> zero_offset_north{0};
    std::atomic<std::int64_t> zero_offset_east{0};
    std::atomic<std::int64_t> zero_offset_vertical{0};
    std::atomic<CalibrationPhase> calibration_phase{CalibrationPhase::idle};
    std::atomic<CalibrationOutcome> calibration_outcome{CalibrationOutcome::none};
    std::atomic<std::uint32_t> calibration_progress{0};
    std::atomic<std::uint64_t> calibration_serial{0};
    std::atomic<std::uint64_t> updated_realtime_ns{0};

    void set_fatal(FatalCode code) noexcept;
};

[[nodiscard]] const char* state_name(RecorderState state) noexcept;
[[nodiscard]] const char* fatal_code_name(FatalCode code) noexcept;
[[nodiscard]] const char* calibration_phase_name(CalibrationPhase phase) noexcept;
[[nodiscard]] const char* calibration_outcome_name(CalibrationOutcome outcome) noexcept;
[[nodiscard]] std::string health_json(const HealthCounters& health);
[[nodiscard]] bool write_health_file(const std::filesystem::path& path,
                                     const HealthCounters& health,
                                     std::string& error);

}  // namespace shakepie
