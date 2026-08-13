#include "shakepie/acquisition.hpp"

#include <algorithm>
#include <cstdlib>
#include <limits>

namespace shakepie {
namespace {

constexpr std::int64_t kSampleMinimum = -8'388'608;
constexpr std::int64_t kSampleMaximum = 8'388'607;

std::int32_t clamp_sample(std::int64_t value) noexcept {
    if (value < kSampleMinimum) return static_cast<std::int32_t>(kSampleMinimum);
    if (value > kSampleMaximum) return static_cast<std::int32_t>(kSampleMaximum);
    return static_cast<std::int32_t>(value);
}

std::int64_t rounded_mean(std::int64_t sum, std::int64_t count) noexcept {
    const auto bias = count / 2;
    return (sum >= 0 ? sum + bias : sum - bias) / count;
}

}  // namespace

AcquisitionEngine::AcquisitionEngine(AcquisitionConfig config,
                                     CalibrationConfig calibration_config,
                                     ISampleSource& source,
                                     IClock& clock,
                                     SpscRing<protocol::SampleBlock>& disk_queue,
                                     LiveOverwriteRing& live_queue,
                                     HealthCounters& health,
                                     std::atomic<bool>& stop_requested,  // NOLINT(bugprone-easily-swappable-parameters)
                                     std::atomic<bool>& acquisition_done,
                                     std::atomic<bool>& calibration_requested,
                                     std::atomic<bool>& calibration_clear,
                                     std::atomic<bool>& calibration_startup) noexcept
    : config_(config),
      calibration_config_(calibration_config),
      source_(source),
      clock_(clock),
      disk_queue_(disk_queue),
      live_queue_(live_queue),
      health_(health),
      stop_requested_(stop_requested),
      acquisition_done_(acquisition_done),
      calibration_requested_(calibration_requested),
      calibration_clear_(calibration_clear),
      calibration_startup_(calibration_startup) {
    // Restored offsets are seeded into the health counters before the thread starts;
    // adopt them so a restart keeps recording centered without a new calibration.
    offset_north_ = health_.zero_offset_north.load(std::memory_order_relaxed);
    offset_east_ = health_.zero_offset_east.load(std::memory_order_relaxed);
    offset_vertical_ = health_.zero_offset_vertical.load(std::memory_order_relaxed);
}

bool AcquisitionEngine::publish(protocol::SampleBlock& block) noexcept {
    block.header.block_sequence = block_sequence_++;
    const auto predicted_depth = disk_queue_.size() + 1U;
    const auto pressure_threshold = disk_queue_.capacity() - (disk_queue_.capacity() / 5U);
    if (predicted_depth >= pressure_threshold) {
        block.header.status_flags |=
            static_cast<std::uint32_t>(protocol::StatusFlag::storage_pressure);
    }
    if (!disk_queue_.try_push(block)) {
        health_.set_fatal(FatalCode::disk_queue_full);
        stop_requested_.store(true, std::memory_order_release);
        return false;
    }
    const auto depth = disk_queue_.size();
    health_.disk_queue_depth.store(depth, std::memory_order_relaxed);
    auto previous = health_.disk_queue_high_water.load(std::memory_order_relaxed);
    while (depth > previous && !health_.disk_queue_high_water.compare_exchange_weak(previous, depth)) {}

    const auto live = live_queue_.push(block);
    if (live.dropped_oldest || !live.stored) {
        health_.live_queue_drops.fetch_add(1U, std::memory_order_relaxed);
    }
    health_.produced_blocks.fetch_add(1U, std::memory_order_relaxed);
    health_.last_block_sequence.store(block.header.block_sequence, std::memory_order_relaxed);
    health_.last_sample_sequence.store(
        block.header.first_sample_sequence + block.header.sample_count - 1U, std::memory_order_relaxed);
    return true;
}

void AcquisitionEngine::handle_calibration_request(bool clear_offsets,
                                                   bool use_startup_window) noexcept {
    health_.calibration_serial.fetch_add(1U, std::memory_order_relaxed);
    if (!calibration_config_.enabled) {
        health_.calibration_phase.store(CalibrationPhase::idle, std::memory_order_relaxed);
        health_.calibration_outcome.store(CalibrationOutcome::disabled, std::memory_order_relaxed);
        return;
    }
    sum_north_ = 0;
    sum_east_ = 0;
    sum_vertical_ = 0;
    window_count_ = 0;
    health_.calibration_progress.store(0, std::memory_order_relaxed);
    if (clear_offsets) {
        measuring_ = false;
        offset_north_ = 0;
        offset_east_ = 0;
        offset_vertical_ = 0;
        health_.zero_offset_north.store(0, std::memory_order_relaxed);
        health_.zero_offset_east.store(0, std::memory_order_relaxed);
        health_.zero_offset_vertical.store(0, std::memory_order_relaxed);
        health_.calibration_phase.store(CalibrationPhase::idle, std::memory_order_relaxed);
        health_.calibration_outcome.store(CalibrationOutcome::cleared, std::memory_order_relaxed);
        return;
    }
    measuring_ = true;
    const auto rate = config_.nominal_group_rate_hz;
    // The automatic startup calibration uses its own long window for a precise zero;
    // manual requests use the short window so they finish quickly.
    const auto window_seconds =
        use_startup_window ? calibration_config_.startup_window_seconds
                           : calibration_config_.window_seconds;
    window_total_ = static_cast<std::uint64_t>(window_seconds) * rate;
    health_.calibration_phase.store(CalibrationPhase::measuring, std::memory_order_relaxed);
    health_.calibration_outcome.store(CalibrationOutcome::none, std::memory_order_relaxed);
}

void AcquisitionEngine::finish_measurement() noexcept {
    measuring_ = false;
    const auto count = static_cast<std::int64_t>(window_count_);
    const auto mean_north = rounded_mean(sum_north_, count);
    const auto mean_east = rounded_mean(sum_east_, count);
    const auto mean_vertical = rounded_mean(sum_vertical_, count);
    const auto limit = static_cast<std::int64_t>(calibration_config_.max_offset_counts);
    if (std::abs(mean_north) > limit || std::abs(mean_east) > limit ||
        std::abs(mean_vertical) > limit) {
        health_.calibration_phase.store(CalibrationPhase::failed, std::memory_order_relaxed);
        health_.calibration_outcome.store(CalibrationOutcome::rejected, std::memory_order_relaxed);
        health_.calibration_progress.store(0, std::memory_order_relaxed);
        return;
    }
    offset_north_ = mean_north;
    offset_east_ = mean_east;
    offset_vertical_ = mean_vertical;
    health_.zero_offset_north.store(mean_north, std::memory_order_relaxed);
    health_.zero_offset_east.store(mean_east, std::memory_order_relaxed);
    health_.zero_offset_vertical.store(mean_vertical, std::memory_order_relaxed);
    health_.calibration_phase.store(CalibrationPhase::idle, std::memory_order_relaxed);
    health_.calibration_outcome.store(CalibrationOutcome::applied, std::memory_order_relaxed);
    health_.calibration_progress.store(100, std::memory_order_relaxed);
}

void AcquisitionEngine::run() noexcept {
    protocol::SampleBlock block{};
    std::uint32_t count = 0;
    std::uint32_t consecutive_errors = 0;
    std::uint64_t previous_realtime = 0;
    std::uint64_t previous_monotonic = 0;
    std::uint64_t previous_group_monotonic = 0;
    const auto nominal_period_ns = 1'000'000'000ULL / config_.nominal_group_rate_hz;

    while (!stop_requested_.load(std::memory_order_acquire)) {
        if (count == 0U) {
            const auto clocks = clock_.snapshot();
            if (clocks.realtime_ns == 0U || clocks.monotonic_raw_ns == 0U) {
                health_.set_fatal(FatalCode::clock_fault);
                stop_requested_.store(true, std::memory_order_release);
                break;
            }
            if ((previous_monotonic != 0U && clocks.monotonic_raw_ns < previous_monotonic) ||
                (previous_group_monotonic != 0U &&
                 clocks.monotonic_raw_ns < previous_group_monotonic)) {
                health_.set_fatal(FatalCode::clock_fault);
                stop_requested_.store(true, std::memory_order_release);
                break;
            }
            if (calibration_requested_.exchange(false, std::memory_order_acq_rel)) {
                // Consume the startup marker atomically with the request so a stale
                // marker can never give a later manual request the long window.
                const auto startup =
                    calibration_startup_.exchange(false, std::memory_order_acq_rel);
                handle_calibration_request(false, startup);
            } else if (calibration_clear_.exchange(false, std::memory_order_acq_rel)) {
                calibration_startup_.store(false, std::memory_order_relaxed);
                handle_calibration_request(true, false);
            }
            block = {};
            block.header.first_sample_sequence = sample_sequence_;
            block.header.utc_start_nanoseconds = clocks.realtime_ns;
            block.header.monotonic_start_nanoseconds = clocks.monotonic_raw_ns;
            block.header.nominal_group_rate_millihz = config_.nominal_group_rate_hz * 1000U;
            if (previous_realtime != 0U) {
                const auto mono_delta = clocks.monotonic_raw_ns - previous_monotonic;
                const bool realtime_went_backwards = clocks.realtime_ns < previous_realtime;
                const auto real_delta = realtime_went_backwards ? 0U : clocks.realtime_ns - previous_realtime;
                const auto difference = real_delta > mono_delta ? real_delta - mono_delta : mono_delta - real_delta;
                if (realtime_went_backwards || difference > 100'000'000ULL) {
                    block.header.timing_flags |= static_cast<std::uint32_t>(protocol::TimingFlag::utc_clock_step);
                }
            }
            previous_realtime = clocks.realtime_ns;
            previous_monotonic = clocks.monotonic_raw_ns;
        }

        protocol::SampleGroup group{};
        const auto result = source_.read_group(group);
        if (result.status != SampleReadStatus::ok) {
            ++consecutive_errors;
            if (result.status == SampleReadStatus::timeout) {
                health_.adc_timeouts.fetch_add(1U, std::memory_order_relaxed);
                block.header.status_flags |= static_cast<std::uint32_t>(protocol::StatusFlag::adc_timeout_seen);
            } else {
                health_.adc_faults.fetch_add(1U, std::memory_order_relaxed);
                block.header.status_flags |= static_cast<std::uint32_t>(protocol::StatusFlag::adc_fault_seen);
            }
            if (result.sequence_advance != 0U) {
                if (count != 0U) {
                    block.header.sample_count = count;
                    if (!publish(block)) break;
                    count = 0;
                }
                sample_sequence_ += result.sequence_advance;
                health_.missing_sample_groups.fetch_add(result.sequence_advance,
                                                        std::memory_order_relaxed);
            }
            if (consecutive_errors >= config_.max_consecutive_adc_errors) {
                health_.set_fatal(FatalCode::adc_fault);
                stop_requested_.store(true, std::memory_order_release);
            }
            continue;
        }
        consecutive_errors = 0;
        block.header.status_flags |= result.status_flags;
        block.header.timing_flags |= result.timing_flags;
        // Calibration accumulates the RAW values while the recording keeps using the
        // previously applied offsets; the new offsets replace the old ones only when
        // the measurement window completes, so the data stream never jumps mid-window.
        if (measuring_) {
            sum_north_ += group.north_count;
            sum_east_ += group.east_count;
            sum_vertical_ += group.vertical_count;
            ++window_count_;
            health_.calibration_progress.store(
                static_cast<std::uint32_t>(window_count_ * 100U / window_total_),
                std::memory_order_relaxed);
            if (window_count_ >= window_total_) {
                finish_measurement();
            }
        }
        if (offset_north_ != 0 || offset_east_ != 0 || offset_vertical_ != 0) {
            const auto centered_north = static_cast<std::int64_t>(group.north_count) - offset_north_;
            const auto centered_east = static_cast<std::int64_t>(group.east_count) - offset_east_;
            const auto centered_vertical =
                static_cast<std::int64_t>(group.vertical_count) - offset_vertical_;
            if (centered_north < kSampleMinimum || centered_north > kSampleMaximum ||
                centered_east < kSampleMinimum || centered_east > kSampleMaximum ||
                centered_vertical < kSampleMinimum || centered_vertical > kSampleMaximum) {
                block.header.status_flags |=
                    static_cast<std::uint32_t>(protocol::StatusFlag::clipping_seen);
            }
            group.north_count = clamp_sample(centered_north);
            group.east_count = clamp_sample(centered_east);
            group.vertical_count = clamp_sample(centered_vertical);
        }
        const auto now = clock_.monotonic_raw_ns();
        if (now == 0U) {
            health_.set_fatal(FatalCode::clock_fault);
            stop_requested_.store(true, std::memory_order_release);
            break;
        }
        if (now < block.header.monotonic_start_nanoseconds ||
            (previous_group_monotonic != 0U && now < previous_group_monotonic)) {
            health_.set_fatal(FatalCode::clock_fault);
            stop_requested_.store(true, std::memory_order_release);
            break;
        }
        if (previous_group_monotonic != 0U &&
            now - previous_group_monotonic > nominal_period_ns * 2U) {
            block.header.timing_flags |=
                static_cast<std::uint32_t>(protocol::TimingFlag::acquisition_late);
        }
        previous_group_monotonic = now;
        const auto offset = now - block.header.monotonic_start_nanoseconds;
        if (offset > std::numeric_limits<std::uint32_t>::max()) {
            group.nanoseconds_from_block_start = std::numeric_limits<std::uint32_t>::max();
            block.header.timing_flags |= static_cast<std::uint32_t>(protocol::TimingFlag::timestamp_saturated);
        } else {
            group.nanoseconds_from_block_start = static_cast<std::uint32_t>(offset);
        }
        block.samples[count++] = group;
        ++sample_sequence_;
        health_.acquired_groups.fetch_add(1U, std::memory_order_relaxed);
        bool force_boundary = false;
        if (result.sequence_advance > 1U) {
            const auto skipped = result.sequence_advance - 1U;
            sample_sequence_ += skipped;
            health_.simulated_sequence_gaps.fetch_add(skipped, std::memory_order_relaxed);
            force_boundary = true;
        }
        if (count == config_.samples_per_block || force_boundary) {
            block.header.sample_count = count;
            if (!publish(block)) break;
            count = 0;
        }
    }

    if (count != 0U && health_.fatal_code.load(std::memory_order_relaxed) != FatalCode::disk_queue_full) {
        block.header.sample_count = count;
        static_cast<void>(publish(block));
    }
    acquisition_done_.store(true, std::memory_order_release);
}

}  // namespace shakepie
