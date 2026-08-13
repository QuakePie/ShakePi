#include "test_harness.hpp"

#include "shakepie/acquisition.hpp"
#include "shakepie/config.hpp"
#include "shakepie/queues.hpp"

#include <atomic>
#include <string>
#include <thread>

namespace {

class SteadyClock final : public shakepie::IClock {
public:
    std::uint64_t monotonic_raw_ns() noexcept override { return now += 5'000'000ULL; }
    std::uint64_t realtime_ns() noexcept override { return 1'700'000'000'000'000'000ULL + now; }
    shakepie::ClockSnapshot snapshot() noexcept override { return {realtime_ns(), now}; }
    std::uint64_t now{1'000'000};
};

// Emits a constant value per channel so the measured mean is exact, and drives the
// calibration request and stop flags from inside the acquisition loop.
class OffsetSource final : public shakepie::ISampleSource {
public:
    OffsetSource(std::atomic<bool>& stop, std::atomic<bool>& request,
                 std::atomic<bool>& clear_request, std::int32_t value,
                 std::uint32_t request_at_group, std::uint32_t stop_at_group,
                 bool request_clear = false)
        : stop_(stop), request_(request), clear_request_(clear_request), value_(value),
          request_at_(request_at_group), stop_at_(stop_at_group), request_clear_(request_clear) {}

    bool initialize(std::string&) override { return true; }

    shakepie::SampleReadResult read_group(shakepie::protocol::SampleGroup& group) noexcept override {
        ++groups_;
        if (groups_ == request_at_) {
            (request_clear_ ? clear_request_ : request_).store(true, std::memory_order_release);
        }
        if (groups_ >= stop_at_) {
            stop_.store(true, std::memory_order_release);
        }
        group.north_count = value_;
        group.east_count = value_;
        group.vertical_count = value_;
        return {};
    }

private:
    std::atomic<bool>& stop_;
    std::atomic<bool>& request_;
    std::atomic<bool>& clear_request_;
    std::int32_t value_;
    std::uint32_t request_at_;
    std::uint32_t stop_at_;
    bool request_clear_;
    std::uint32_t groups_{0};
};

shakepie::AcquisitionConfig engine_config(std::uint32_t samples_per_block) {
    shakepie::AcquisitionConfig value{};
    value.samples_per_block = samples_per_block;
    value.nominal_group_rate_hz = 200;
    return value;
}

shakepie::CalibrationConfig calibration_config() {
    shakepie::CalibrationConfig value{};
    value.enabled = true;
    value.window_seconds = 1;  // 200 groups at the 200 Hz test rate
    value.max_offset_counts = 4'000'000;
    return value;
}

std::int32_t last_block_sample(shakepie::SpscRing<shakepie::protocol::SampleBlock>& disk) {
    shakepie::protocol::SampleBlock popped{};
    std::int32_t last = -999'999'999;
    while (disk.try_pop(popped)) {
        last = popped.samples[popped.header.sample_count - 1].north_count;
    }
    return last;
}

}  // namespace

int main() {
    // A calibration request measures the DC offset and recenters subsequent data.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(200);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 12'345, 100U, 600U);
        shakepie::AcquisitionEngine engine(engine_config(4), calibration_config(), source, clock,
                                           disk, live, health, stop, done, request, clear,
                                           startup);
        engine.run();
        CHECK(done.load());
        CHECK(health.calibration_outcome.load() == shakepie::CalibrationOutcome::applied);
        CHECK(health.calibration_phase.load() == shakepie::CalibrationPhase::idle);
        CHECK(health.zero_offset_north.load() == 12'345);
        CHECK(health.zero_offset_east.load() == 12'345);
        CHECK(health.zero_offset_vertical.load() == 12'345);
        CHECK(health.calibration_progress.load() == 100U);
        CHECK(last_block_sample(disk) == 0);  // recorded data is centered after calibration
    }

    // A measured mean beyond the configured limit is rejected and offsets stay zero.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(200);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 5'000, 100U, 600U);
        auto limits = calibration_config();
        limits.max_offset_counts = 100;
        shakepie::AcquisitionEngine engine(engine_config(4), limits, source, clock, disk, live,
                                           health, stop, done, request, clear, startup);
        engine.run();
        CHECK(health.calibration_outcome.load() == shakepie::CalibrationOutcome::rejected);
        CHECK(health.calibration_phase.load() == shakepie::CalibrationPhase::failed);
        CHECK(health.zero_offset_north.load() == 0);
        CHECK(last_block_sample(disk) == 5'000);  // data untouched after a rejected calibration
    }

    // A clear request zeroes existing offsets immediately.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        health.zero_offset_north.store(500, std::memory_order_relaxed);
        health.zero_offset_east.store(500, std::memory_order_relaxed);
        health.zero_offset_vertical.store(500, std::memory_order_relaxed);
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(64);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 2'000, 20U, 200U, true);
        shakepie::AcquisitionEngine engine(engine_config(4), calibration_config(), source, clock,
                                           disk, live, health, stop, done, request, clear,
                                           startup);
        engine.run();
        CHECK(health.calibration_outcome.load() == shakepie::CalibrationOutcome::cleared);
        CHECK(health.zero_offset_north.load() == 0);
        CHECK(last_block_sample(disk) == 2'000);  // raw values again after clearing
    }

    // Restored offsets (seeded before the engine starts) recenter data without a request.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        health.zero_offset_north.store(-700, std::memory_order_relaxed);
        health.zero_offset_east.store(-700, std::memory_order_relaxed);
        health.zero_offset_vertical.store(-700, std::memory_order_relaxed);
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(64);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 300, 9'999U, 100U);  // never requests
        shakepie::AcquisitionEngine engine(engine_config(4), calibration_config(), source, clock,
                                           disk, live, health, stop, done, request, clear,
                                           startup);
        engine.run();
        CHECK(health.calibration_outcome.load() == shakepie::CalibrationOutcome::none);
        CHECK(last_block_sample(disk) == 1'000);  // 300 - (-700)
    }

    // Offset subtraction saturates at the 24-bit boundaries and raises the clipping flag.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        health.zero_offset_vertical.store(-5, std::memory_order_relaxed);
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(8);
        shakepie::LiveOverwriteRing live(4);
        OffsetSource source(stop, request, clear, 8'388'607, 9'999U, 8U);
        shakepie::AcquisitionEngine engine(engine_config(4), calibration_config(), source, clock,
                                           disk, live, health, stop, done, request, clear,
                                           startup);
        engine.run();
        shakepie::protocol::SampleBlock popped{};
        bool saw_clipping = false;
        std::int32_t last_sample = 0;
        while (disk.try_pop(popped)) {
            saw_clipping = saw_clipping ||
                (popped.header.status_flags &
                 static_cast<std::uint32_t>(shakepie::protocol::StatusFlag::clipping_seen)) != 0U;
            last_sample = popped.samples[popped.header.sample_count - 1].vertical_count;
        }
        CHECK(saw_clipping);
        CHECK(last_sample == 8'388'607);  // 8388607 - (-5) saturates
    }

    // A disabled calibration ignores requests and leaves data untouched.
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(64);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 1'111, 20U, 200U);
        auto disabled = calibration_config();
        disabled.enabled = false;
        shakepie::AcquisitionEngine engine(engine_config(4), disabled, source, clock, disk, live,
                                           health, stop, done, request, clear, startup);
        engine.run();
        CHECK(health.calibration_outcome.load() == shakepie::CalibrationOutcome::disabled);
        CHECK(health.zero_offset_north.load() == 0);
        CHECK(last_block_sample(disk) == 1'111);
    }

    // The startup marker selects the long startup window for that one measurement, and
    // is consumed with the request (a later manual request uses the short window again).
    {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::atomic<bool> request{false};
        std::atomic<bool> clear{false};
        std::atomic<bool> startup{false};
        shakepie::HealthCounters health;
        SteadyClock clock;
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(64);
        shakepie::LiveOverwriteRing live(8);
        OffsetSource source(stop, request, clear, 400, 10U, 30U);
        auto limits = calibration_config();
        limits.startup_window_seconds = 300;  // 60,000 groups at the 200 Hz test rate
        shakepie::AcquisitionEngine engine(engine_config(4), limits, source, clock, disk, live,
                                           health, stop, done, request, clear, startup);
        startup.store(true, std::memory_order_release);
        request.store(true, std::memory_order_release);
        engine.run();
        // Only 30 groups ran: with the long window the measurement cannot have finished.
        CHECK(health.calibration_phase.load() == shakepie::CalibrationPhase::measuring);
        CHECK(health.zero_offset_north.load() == 0);
        CHECK(health.calibration_progress.load() < 100U);
    }
    return shakepie_test_failures == 0 ? 0 : 1;
}
