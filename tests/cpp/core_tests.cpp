#include "test_harness.hpp"

#include "shakepie/acquisition.hpp"
#include "shakepie/config.hpp"
#include "shakepie/fake_adc.hpp"
#include "shakepie/queues.hpp"

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>

namespace {

class TestClock final : public shakepie::IClock {
public:
    std::uint64_t monotonic_raw_ns() noexcept override { return monotonic += 10U; }
    std::uint64_t realtime_ns() noexcept override { return realtime; }
    shakepie::ClockSnapshot snapshot() noexcept override {
        if (snapshot_count++ == 0U) return {1'000'000'000U, 1'000U};
        return {500'000'000U, 2'020U}; // deliberate backwards UTC step
    }
    std::uint64_t monotonic{2'000};
    std::uint64_t realtime{1'000'000'000};
    unsigned snapshot_count{};
};

class TwoSampleSource final : public shakepie::ISampleSource {
public:
    explicit TwoSampleSource(std::atomic<bool>& stop) : stop_(stop) {}
    bool initialize(std::string&) override { return true; }
    shakepie::SampleReadResult read_group(shakepie::protocol::SampleGroup& group) noexcept override {
        group.north_count = static_cast<std::int32_t>(count_ + 1U);
        group.east_count = static_cast<std::int32_t>(count_ + 2U);
        group.vertical_count = static_cast<std::int32_t>(count_ + 3U);
        ++count_;
        if (count_ == 2U) stop_.store(true);
        return {shakepie::SampleReadStatus::ok, 0U, 0U, count_ == 1U ? 2U : 1U};
    }
private:
    std::atomic<bool>& stop_;
    unsigned count_{};
};

class AdvancingClock final : public shakepie::IClock {
public:
    std::uint64_t monotonic_raw_ns() noexcept override { return now += 1'000'000'000ULL; }
    std::uint64_t realtime_ns() noexcept override { return 1'700'000'000'000'000'000ULL + now; }
    shakepie::ClockSnapshot snapshot() noexcept override {
        const auto monotonic = monotonic_raw_ns();
        return {1'700'000'000'000'000'000ULL + monotonic, monotonic};
    }
    std::uint64_t now{};
};

class EndlessSource final : public shakepie::ISampleSource {
public:
    bool initialize(std::string&) override { return true; }
    shakepie::SampleReadResult read_group(shakepie::protocol::SampleGroup& group) noexcept override {
        group.north_count = static_cast<std::int32_t>(sequence_++);
        return {};
    }
private:
    std::uint32_t sequence_{};
};

class TimeoutBetweenSamples final : public shakepie::ISampleSource {
public:
    explicit TimeoutBetweenSamples(std::atomic<bool>& stop) : stop_(stop) {}
    bool initialize(std::string&) override { return true; }
    shakepie::SampleReadResult read_group(shakepie::protocol::SampleGroup&) noexcept override {
        ++call_;
        if (call_ == 2U) return {shakepie::SampleReadStatus::timeout, 0U, 0U, 1U};
        if (call_ == 3U) stop_.store(true, std::memory_order_release);
        return {};
    }
private:
    std::atomic<bool>& stop_;
    unsigned call_{};
};

std::atomic<bool> calibration_requested{false};
std::atomic<bool> calibration_clear{false};
std::atomic<bool> calibration_startup{false};

shakepie::CalibrationConfig calibration_config() {
    shakepie::CalibrationConfig value{};
    value.enabled = false;  // existing engine tests are unaffected by calibration
    return value;
}

shakepie::protocol::SampleBlock block(std::uint64_t sequence) {
    shakepie::protocol::SampleBlock value{};
    value.header.block_sequence = sequence;
    value.header.sample_count = 1;
    return value;
}

}  // namespace

int main() {
    shakepie::RecorderConfig config{};
#if defined(_WIN32)
    config.storage.hdd_mount = "C:\\shakepie-test-mount";
    config.storage.data_directory = "C:\\shakepie-test-mount\\recordings";
#endif
    CHECK(shakepie::validate_config(config).empty());
    config.storage.queue_seconds = 299;
    CHECK(!shakepie::validate_config(config).empty());
    config.storage.queue_seconds = 300;
    config.stream.bind_address = "0.0.0.0";
    CHECK(!shakepie::validate_config(config).empty());
    config.stream.bind_address = "127.0.0.1";
    config.acquisition.nominal_group_rate_hz = 1;
    CHECK(!shakepie::validate_config(config).empty());
    config.acquisition.nominal_group_rate_hz = 750;

    shakepie::SpscRing<int> ring(3);
    CHECK(ring.try_push(1));
    CHECK(ring.try_push(2));
    CHECK(ring.try_push(3));
    CHECK(!ring.try_push(4));
    int item = 0;
    CHECK(ring.try_pop(item) && item == 1);
    CHECK(ring.try_push(4));
    CHECK(ring.try_pop(item) && item == 2);
    CHECK(ring.try_pop(item) && item == 3);
    CHECK(ring.try_pop(item) && item == 4);

    shakepie::LiveOverwriteRing live(2);
    CHECK(!live.push(block(1)).dropped_oldest);
    CHECK(!live.push(block(2)).dropped_oldest);
    CHECK(live.push(block(3)).dropped_oldest);
    shakepie::protocol::SampleBlock popped{};
    CHECK(live.try_pop(popped) && popped.header.block_sequence == 2U);
    CHECK(live.try_pop(popped) && popped.header.block_sequence == 3U);

    shakepie::LiveOverwriteRing concurrent_live(16);
    std::atomic<bool> producer_done{false};
    std::thread producer([&] {
        for (std::uint64_t sequence = 0; sequence < 100'000U; ++sequence) {
            static_cast<void>(concurrent_live.push(block(sequence)));
        }
        producer_done.store(true, std::memory_order_release);
    });
    std::uint64_t last_sequence = 0;
    bool have_sequence = false;
    const auto accept_sequence = [&](std::uint64_t sequence) {
        const bool ordered = !have_sequence || sequence > last_sequence;
        if (!ordered) {
            std::cerr << "live sequence regression: " << last_sequence << " -> " << sequence << '\n';
        }
        CHECK(ordered);
        last_sequence = sequence;
        have_sequence = true;
    };
    while (!producer_done.load(std::memory_order_acquire)) {
        if (concurrent_live.try_pop(popped)) {
            accept_sequence(popped.header.block_sequence);
        }
    }
    while (concurrent_live.try_pop(popped)) {
        accept_sequence(popped.header.block_sequence);
    }
    producer.join();
    CHECK(have_sequence);

    std::atomic<bool> stop{false};
    std::atomic<bool> done{false};
    TwoSampleSource source(stop);
    TestClock clock;
    shakepie::SpscRing<shakepie::protocol::SampleBlock> disk(8);
    shakepie::LiveOverwriteRing live_acquisition(4);
    shakepie::HealthCounters health;
    shakepie::AcquisitionConfig acquisition_config{};
    acquisition_config.samples_per_block = 2;
    shakepie::AcquisitionEngine engine(acquisition_config, calibration_config(), source, clock,
                                       disk, live_acquisition, health, stop, done,
                                       calibration_requested, calibration_clear,
                                       calibration_startup);
    engine.run();
    CHECK(done.load());
    shakepie::protocol::SampleBlock first{};
    shakepie::protocol::SampleBlock second{};
    CHECK(disk.try_pop(first));
    CHECK(disk.try_pop(second));
    CHECK(first.header.block_sequence == 0U && first.header.first_sample_sequence == 0U);
    CHECK(first.header.sample_count == 1U); // forced boundary makes the simulated gap visible
    CHECK(second.header.block_sequence == 1U && second.header.first_sample_sequence == 2U);
    CHECK((second.header.timing_flags &
           static_cast<std::uint32_t>(shakepie::protocol::TimingFlag::utc_clock_step)) != 0U);
    CHECK(health.simulated_sequence_gaps.load() == 1U);

    std::atomic<bool> gap_stop{false};
    std::atomic<bool> gap_done{false};
    TimeoutBetweenSamples gap_source(gap_stop);
    AdvancingClock gap_clock;
    shakepie::SpscRing<shakepie::protocol::SampleBlock> gap_disk(8);
    shakepie::LiveOverwriteRing gap_live(4);
    shakepie::HealthCounters gap_health;
    shakepie::AcquisitionConfig gap_config{};
    gap_config.samples_per_block = 64;
    shakepie::AcquisitionEngine gap_engine(gap_config, calibration_config(), gap_source, gap_clock,
                                           gap_disk, gap_live, gap_health, gap_stop, gap_done,
                                           calibration_requested, calibration_clear,
                                           calibration_startup);
    gap_engine.run();
    CHECK(gap_health.adc_timeouts.load() == 1U);
    CHECK(gap_health.missing_sample_groups.load() == 1U);
    CHECK(gap_disk.try_pop(first));
    CHECK(gap_disk.try_pop(second));
    CHECK(first.header.first_sample_sequence == 0U && first.header.sample_count == 1U);
    CHECK(second.header.first_sample_sequence == 2U && second.header.sample_count == 1U);

    shakepie::FakeConfig fake_config{};
    fake_config.noise_amplitude = 0;
    fake_config.impulse_every = 2;
    fake_config.clip_every = 3;
    fake_config.gap_every = 4;
    fake_config.timeout_every = 5;
    fake_config.jitter_ns = 100;
    AdvancingClock fake_clock;
    shakepie::FakeAdc fake(fake_config, 750, fake_clock);
    std::string fake_error;
    CHECK(fake.initialize(fake_error));
    shakepie::protocol::SampleGroup fake_group{};
    CHECK(fake.read_group(fake_group).status == shakepie::SampleReadStatus::ok);
    CHECK(fake.read_group(fake_group).status == shakepie::SampleReadStatus::ok);
    CHECK(fake_group.vertical_count == 6'000'000);
    const auto clipped = fake.read_group(fake_group);
    CHECK(clipped.status == shakepie::SampleReadStatus::ok);
    CHECK(fake_group.north_count == 8'388'607 && fake_group.east_count == -8'388'608);
    CHECK(fake.read_group(fake_group).sequence_advance == 2U);
    CHECK(fake.read_group(fake_group).status == shakepie::SampleReadStatus::timeout);

    std::atomic<bool> overflow_stop{false};
    std::atomic<bool> overflow_done{false};
    EndlessSource endless;
    AdvancingClock overflow_clock;
    shakepie::SpscRing<shakepie::protocol::SampleBlock> tiny_disk(2);
    shakepie::LiveOverwriteRing overflow_live(2);
    shakepie::HealthCounters overflow_health;
    shakepie::AcquisitionConfig one_sample_blocks{};
    one_sample_blocks.samples_per_block = 1;
    shakepie::AcquisitionEngine overflow_engine(
        one_sample_blocks, calibration_config(), endless, overflow_clock, tiny_disk,
        overflow_live, overflow_health, overflow_stop, overflow_done, calibration_requested,
        calibration_clear, calibration_startup);
    overflow_engine.run();
    CHECK(overflow_done.load());
    CHECK(overflow_health.fatal_code.load() == shakepie::FatalCode::disk_queue_full);
    CHECK(tiny_disk.size() == 2U);
    return shakepie_test_failures == 0 ? 0 : 1;
}
