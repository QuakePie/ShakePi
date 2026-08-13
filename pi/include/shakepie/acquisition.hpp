#pragma once

#include "shakepie/clocks.hpp"
#include "shakepie/config.hpp"
#include "shakepie/health.hpp"
#include "shakepie/queues.hpp"
#include "shakepie/sample_source.hpp"

#include <atomic>

namespace shakepie {

class AcquisitionEngine {
public:
    AcquisitionEngine(AcquisitionConfig config,
                      CalibrationConfig calibration_config,
                      ISampleSource& source,
                      IClock& clock,
                      SpscRing<protocol::SampleBlock>& disk_queue,
                      LiveOverwriteRing& live_queue,
                      HealthCounters& health,
                      std::atomic<bool>& stop_requested,
                      std::atomic<bool>& acquisition_done,
                      std::atomic<bool>& calibration_requested,
                      std::atomic<bool>& calibration_clear,
                      std::atomic<bool>& calibration_startup) noexcept;

    void run() noexcept;

private:
    [[nodiscard]] bool publish(protocol::SampleBlock& block) noexcept;
    void handle_calibration_request(bool clear_offsets, bool use_startup_window) noexcept;
    void finish_measurement() noexcept;

    AcquisitionConfig config_;
    CalibrationConfig calibration_config_;
    ISampleSource& source_;
    IClock& clock_;
    SpscRing<protocol::SampleBlock>& disk_queue_;
    LiveOverwriteRing& live_queue_;
    HealthCounters& health_;
    std::atomic<bool>& stop_requested_;
    std::atomic<bool>& acquisition_done_;
    std::atomic<bool>& calibration_requested_;
    std::atomic<bool>& calibration_clear_;
    std::atomic<bool>& calibration_startup_;
    std::uint64_t block_sequence_{0};
    std::uint64_t sample_sequence_{0};
    bool measuring_{false};
    std::uint64_t window_total_{0};
    std::uint64_t window_count_{0};
    std::int64_t sum_north_{0};
    std::int64_t sum_east_{0};
    std::int64_t sum_vertical_{0};
    std::int64_t offset_north_{0};
    std::int64_t offset_east_{0};
    std::int64_t offset_vertical_{0};
};

}  // namespace shakepie
