#pragma once

#include "shakepie/clocks.hpp"
#include "shakepie/sample_source.hpp"

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <utility>

namespace shakepie {

/// Wraps a sample source so groups are produced at the configured nominal cadence.
///
/// The ADS1256 pipeline delivers conversions faster than most configured group rates;
/// without pacing the recorder stores every DRDY-driven group while labelling the data
/// with the nominal rate, so sample counts and timelines disagree with wall time. The
/// fake ADC already paces itself; this decorator gives hardware sources the same
/// contract: one group per nominal period, measured on CLOCK_MONOTONIC_RAW.
class PacedSampleSource final : public ISampleSource {
public:
    using Sleeper = std::function<void(std::chrono::nanoseconds)>;

    /// `group_rate_hz` of zero disables pacing (pass-through). The sleeper is injectable
    /// for tests; production uses an interruptible sleep so stop signals stay responsive.
    PacedSampleSource(std::unique_ptr<ISampleSource> inner, IClock& clock,
                      std::uint32_t group_rate_hz, Sleeper sleeper = {}) noexcept
        : inner_(std::move(inner)), clock_(clock),
          period_ns_(group_rate_hz == 0U
                         ? std::chrono::nanoseconds::zero()
                         : std::chrono::nanoseconds(1'000'000'000ULL / group_rate_hz)),
          sleeper_(sleeper ? std::move(sleeper) : default_sleeper) {}

    [[nodiscard]] bool initialize(std::string& error) override {
        return inner_->initialize(error);
    }

    [[nodiscard]] SampleReadResult read_group(protocol::SampleGroup& group) noexcept override;

private:
    static void default_sleeper(std::chrono::nanoseconds remaining) noexcept;

    std::unique_ptr<ISampleSource> inner_;
    IClock& clock_;
    std::chrono::nanoseconds period_ns_;
    Sleeper sleeper_;
    std::uint64_t next_due_ns_{0};
};

}  // namespace shakepie
