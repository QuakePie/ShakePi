#include "shakepie/fake_adc.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <thread>

namespace shakepie {

FakeAdc::FakeAdc(FakeConfig config, std::uint32_t group_rate_hz, IClock& clock)
    : config_(config), group_rate_hz_(group_rate_hz), clock_(clock), random_state_(config.seed) {
    constexpr double pi = 3.14159265358979323846;
    for (std::size_t index = 0; index < sine_.size(); ++index) {
        const double phase = 2.0 * pi * static_cast<double>(index) / static_cast<double>(sine_.size());
        sine_[index] = static_cast<std::int32_t>(std::sin(phase) * 2'000'000.0);
    }
}

bool FakeAdc::initialize(std::string& error) {
    if (group_rate_hz_ == 0U) {
        error = "fake ADC group rate must be positive";
        return false;
    }
    next_due_ns_ = clock_.monotonic_raw_ns();
    return true;
}

std::int32_t FakeAdc::noise() noexcept {
    random_state_ ^= random_state_ << 13U;
    random_state_ ^= random_state_ >> 17U;
    random_state_ ^= random_state_ << 5U;
    if (config_.noise_amplitude == 0) return 0;
    const auto span = static_cast<std::uint32_t>(config_.noise_amplitude) * 2U + 1U;
    return static_cast<std::int32_t>(random_state_ % span) - config_.noise_amplitude;
}

void FakeAdc::wait_until_due() noexcept {
    const std::uint64_t period = 1'000'000'000ULL / group_rate_hz_;
    std::int64_t jitter = 0;
    if (config_.jitter_ns != 0U) {
        jitter = static_cast<std::int64_t>(random_state_ % (config_.jitter_ns * 2U + 1U)) -
                 static_cast<std::int64_t>(config_.jitter_ns);
    }
    next_due_ns_ += period;
    const std::uint64_t target = jitter < 0 && static_cast<std::uint64_t>(-jitter) > next_due_ns_
                                     ? 0U
                                     : static_cast<std::uint64_t>(static_cast<std::int64_t>(next_due_ns_) + jitter);
    for (;;) {
        const auto now = clock_.monotonic_raw_ns();
        if (now >= target) return;
        const auto remaining = target - now;
        if (remaining > 100'000U) {
            std::this_thread::sleep_for(std::chrono::nanoseconds(remaining - 50'000U));
        } else {
            std::this_thread::yield();
        }
    }
}

SampleReadResult FakeAdc::read_group(protocol::SampleGroup& group) noexcept {
    wait_until_due();
    ++group_index_;
    if (config_.timeout_every != 0U && group_index_ % config_.timeout_every == 0U) {
        return {SampleReadStatus::timeout,
                static_cast<std::uint32_t>(protocol::StatusFlag::adc_timeout_seen), 0U, 1U};
    }

    const auto sample = [this](std::size_t phase) {
        const auto value = static_cast<std::int64_t>(
                               sine_[(static_cast<std::size_t>(group_index_) + phase) % sine_.size()]) +
                           noise();
        return static_cast<std::int32_t>(
            std::clamp<std::int64_t>(value, -8'388'608, 8'388'607));
    };
    group.north_count = sample(0);
    group.east_count = sample(341);
    group.vertical_count = sample(683);
    std::uint32_t flags = static_cast<std::uint32_t>(protocol::StatusFlag::simulated);
    if (config_.impulse_every != 0U && group_index_ % config_.impulse_every == 0U) {
        group.vertical_count = 6'000'000;
    }
    if (config_.clip_every != 0U && group_index_ % config_.clip_every == 0U) {
        group.north_count = 8'388'607;
        group.east_count = -8'388'608;
        flags |= static_cast<std::uint32_t>(protocol::StatusFlag::clipping_seen);
    }
    const std::uint32_t advance =
        config_.gap_every != 0U && group_index_ % config_.gap_every == 0U ? 2U : 1U;
    return {SampleReadStatus::ok, flags, 0U, advance};
}

}  // namespace shakepie
