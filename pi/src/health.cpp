#include "shakepie/health.hpp"

#include <fstream>
#include <sstream>
#include <system_error>

namespace shakepie {

void HealthCounters::set_fatal(FatalCode code) noexcept {
    FatalCode expected = FatalCode::none;
    fatal_code.compare_exchange_strong(expected, code, std::memory_order_relaxed);
    state.store(RecorderState::fatal, std::memory_order_release);
}

const char* state_name(RecorderState state) noexcept {
    switch (state) {
        case RecorderState::starting: return "starting";
        case RecorderState::running: return "running";
        case RecorderState::stopping: return "stopping";
        case RecorderState::stopped: return "stopped";
        case RecorderState::fatal: return "fatal";
    }
    return "unknown";
}

const char* fatal_code_name(FatalCode code) noexcept {
    switch (code) {
        case FatalCode::none: return "none";
        case FatalCode::disk_queue_full: return "disk_queue_full";
        case FatalCode::adc_fault: return "adc_fault";
        case FatalCode::storage_open: return "storage_open";
        case FatalCode::storage_write: return "storage_write";
        case FatalCode::storage_sync: return "storage_sync";
        case FatalCode::storage_close: return "storage_close";
        case FatalCode::storage_rename: return "storage_rename";
        case FatalCode::hdd_unavailable: return "hdd_unavailable";
        case FatalCode::stream_start: return "stream_start";
        case FatalCode::clock_fault: return "clock_fault";
    }
    return "unknown";
}

const char* calibration_phase_name(CalibrationPhase phase) noexcept {
    switch (phase) {
        case CalibrationPhase::idle: return "idle";
        case CalibrationPhase::measuring: return "measuring";
        case CalibrationPhase::failed: return "failed";
    }
    return "unknown";
}

const char* calibration_outcome_name(CalibrationOutcome outcome) noexcept {
    switch (outcome) {
        case CalibrationOutcome::none: return "none";
        case CalibrationOutcome::applied: return "applied";
        case CalibrationOutcome::cleared: return "cleared";
        case CalibrationOutcome::rejected: return "rejected";
        case CalibrationOutcome::disabled: return "disabled";
    }
    return "unknown";
}

std::string health_json(const HealthCounters& health) {
    std::ostringstream output;
    output << "{\n"
           << "  \"state\": \"" << state_name(health.state.load()) << "\",\n"
           << "  \"fatal_code\": \"" << fatal_code_name(health.fatal_code.load()) << "\",\n"
           << "  \"acquired_groups\": " << health.acquired_groups.load() << ",\n"
           << "  \"produced_blocks\": " << health.produced_blocks.load() << ",\n"
           << "  \"adc_timeouts\": " << health.adc_timeouts.load() << ",\n"
           << "  \"adc_faults\": " << health.adc_faults.load() << ",\n"
           << "  \"missing_sample_groups\": " << health.missing_sample_groups.load() << ",\n"
           << "  \"simulated_sequence_gaps\": " << health.simulated_sequence_gaps.load() << ",\n"
           << "  \"live_queue_drops\": " << health.live_queue_drops.load() << ",\n"
           << "  \"client_queue_drops\": " << health.client_queue_drops.load() << ",\n"
           << "  \"disk_queue_depth\": " << health.disk_queue_depth.load() << ",\n"
           << "  \"disk_queue_high_water\": " << health.disk_queue_high_water.load() << ",\n"
           << "  \"storage_bytes\": " << health.storage_bytes.load() << ",\n"
           << "  \"storage_blocks\": " << health.storage_blocks.load() << ",\n"
           << "  \"storage_errors\": " << health.storage_errors.load() << ",\n"
           << "  \"storage_last_write_latency_us\": " << health.storage_last_write_latency_us.load() << ",\n"
           << "  \"storage_max_write_latency_us\": " << health.storage_max_write_latency_us.load() << ",\n"
           << "  \"disk_free_bytes\": " << health.disk_free_bytes.load() << ",\n"
           << "  \"connected_clients\": " << health.connected_clients.load() << ",\n"
           << "  \"client_connections\": " << health.client_connections.load() << ",\n"
           << "  \"client_disconnects\": " << health.client_disconnects.load() << ",\n"
           << "  \"stream_errors\": " << health.stream_errors.load() << ",\n"
           << "  \"last_block_sequence\": " << health.last_block_sequence.load() << ",\n"
           << "  \"last_sample_sequence\": " << health.last_sample_sequence.load() << ",\n"
           << "  \"zero_offset_north\": " << health.zero_offset_north.load() << ",\n"
           << "  \"zero_offset_east\": " << health.zero_offset_east.load() << ",\n"
           << "  \"zero_offset_vertical\": " << health.zero_offset_vertical.load() << ",\n"
           << "  \"calibration_phase\": \"" << calibration_phase_name(health.calibration_phase.load())
           << "\",\n"
           << "  \"calibration_outcome\": \""
           << calibration_outcome_name(health.calibration_outcome.load()) << "\",\n"
           << "  \"calibration_progress\": " << health.calibration_progress.load() << ",\n"
           << "  \"calibration_serial\": " << health.calibration_serial.load() << ",\n"
           << "  \"updated_realtime_ns\": " << health.updated_realtime_ns.load() << "\n"
           << "}\n";
    return output.str();
}

bool write_health_file(const std::filesystem::path& path,
                       const HealthCounters& health,
                       std::string& error) {
    std::error_code filesystem_error;
    std::filesystem::create_directories(path.parent_path(), filesystem_error);
    if (filesystem_error) {
        error = "cannot create health directory: " + filesystem_error.message();
        return false;
    }
    const auto temporary = path.string() + ".tmp";
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) {
            error = "cannot open temporary health file";
            return false;
        }
        output << health_json(health);
        output.flush();
        if (!output) {
            error = "cannot write temporary health file";
            return false;
        }
    }
    std::filesystem::rename(temporary, path, filesystem_error);
    if (filesystem_error) {
        std::filesystem::remove(path, filesystem_error);
        filesystem_error.clear();
        std::filesystem::rename(temporary, path, filesystem_error);
    }
    if (filesystem_error) {
        error = "cannot publish health file: " + filesystem_error.message();
        return false;
    }
    return true;
}

}  // namespace shakepie
