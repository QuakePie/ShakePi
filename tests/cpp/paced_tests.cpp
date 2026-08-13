#include "test_harness.hpp"

#include "shakepie/config.hpp"
#include "shakepie/paced_source.hpp"

#include <chrono>
#include <string>
#include <vector>

namespace {

class ManualClock final : public shakepie::IClock {
public:
    std::uint64_t monotonic_raw_ns() noexcept override { return now; }
    std::uint64_t realtime_ns() noexcept override { return 0; }
    shakepie::ClockSnapshot snapshot() noexcept override { return {0U, now}; }
    std::uint64_t now{1'000'000};
};

class RecordingSource final : public shakepie::ISampleSource {
public:
    bool initialize(std::string&) override {
        ++initialized;
        return true;
    }
    shakepie::SampleReadResult read_group(shakepie::protocol::SampleGroup& group) noexcept override {
        ++read_calls;
        group.north_count = 42;
        return {status, flags, timing, advance};
    }
    unsigned initialized{};
    unsigned read_calls{};
    shakepie::SampleReadStatus status{shakepie::SampleReadStatus::ok};
    std::uint32_t flags{7};
    std::uint32_t timing{9};
    std::uint32_t advance{1};
};

}  // namespace

int main() {
    // A zero rate disables pacing entirely: pass-through with no sleeps.
    {
        ManualClock clock;
        std::vector<std::chrono::nanoseconds> sleeps;
        shakepie::PacedSampleSource unpaced(
            std::make_unique<RecordingSource>(), clock, 0U,
            [&sleeps](std::chrono::nanoseconds amount) { sleeps.push_back(amount); });
        shakepie::protocol::SampleGroup group{};
        const auto result = unpaced.read_group(group);
        CHECK(result.status == shakepie::SampleReadStatus::ok);
        CHECK(result.status_flags == 7U && result.timing_flags == 9U && result.sequence_advance == 1U);
        CHECK(group.north_count == 42);
        CHECK(sleeps.empty());
    }

    // Paced reads follow the nominal grid: sleep until each due time, then advance.
    {
        ManualClock clock;
        std::vector<std::chrono::nanoseconds> sleeps;
        auto sleeper = [&sleeps, &clock](std::chrono::nanoseconds amount) {
            sleeps.push_back(amount);
            clock.now += static_cast<std::uint64_t>(amount.count());
        };
        std::string error;
        shakepie::PacedSampleSource paced(std::make_unique<RecordingSource>(), clock, 200U, sleeper);
        CHECK(paced.initialize(error));
        shakepie::protocol::SampleGroup group{};
        CHECK(paced.read_group(group).status == shakepie::SampleReadStatus::ok);
        CHECK(sleeps.empty()); // the first group starts immediately
        CHECK(paced.read_group(group).status == shakepie::SampleReadStatus::ok);
        CHECK(sleeps.size() == 1U);
        CHECK(sleeps.front() == std::chrono::nanoseconds(5'000'000));
        CHECK(clock.now == 6'000'000);
        CHECK(paced.read_group(group).status == shakepie::SampleReadStatus::ok);
        CHECK(sleeps.size() == 2U);
        CHECK(sleeps.back() == std::chrono::nanoseconds(5'000'000));
        CHECK(clock.now == 11'000'000);
    }

    // A stalled schedule restarts from the current time instead of bursting.
    {
        ManualClock clock;
        std::vector<std::chrono::nanoseconds> sleeps;
        auto sleeper = [&sleeps, &clock](std::chrono::nanoseconds amount) {
            sleeps.push_back(amount);
            clock.now += static_cast<std::uint64_t>(amount.count());
        };
        shakepie::PacedSampleSource paced(std::make_unique<RecordingSource>(), clock, 200U, sleeper);
        shakepie::protocol::SampleGroup group{};
        static_cast<void>(paced.read_group(group));
        static_cast<void>(paced.read_group(group));
        clock.now = 50'000'000; // the caller stalled far past the scheduled group
        static_cast<void>(paced.read_group(group));
        CHECK(sleeps.size() == 1U);
        static_cast<void>(paced.read_group(group));
        CHECK(sleeps.size() == 2U);
        CHECK(sleeps.back() == std::chrono::nanoseconds(5'000'000));
        CHECK(clock.now == 55'000'000);
    }

    // Hardware configurations must reserve six conversion periods per paced group
    // (three conversions plus one discarded settling conversion per switch).
    {
        shakepie::RecorderConfig config{};
#if defined(_WIN32)
        config.storage.hdd_mount = "C:\\shakepie-test-mount";
        config.storage.data_directory = "C:\\shakepie-test-mount\\recordings";
#endif
        config.adc.data_rate_sps = 2000.0;
        config.acquisition.nominal_group_rate_hz = 800;
        CHECK(!shakepie::validate_config(config).empty());
        config.acquisition.nominal_group_rate_hz = 300;
        CHECK(shakepie::validate_config(config).empty());
        config.adc.backend = shakepie::AdcBackend::fake;
        config.acquisition.nominal_group_rate_hz = 800;
        CHECK(shakepie::validate_config(config).empty());
    }
    return shakepie_test_failures == 0 ? 0 : 1;
}
