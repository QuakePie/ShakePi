#include "shakepie/paced_source.hpp"

#include <thread>

namespace shakepie {

void PacedSampleSource::default_sleeper(std::chrono::nanoseconds remaining) noexcept {
    if (remaining <= std::chrono::nanoseconds::zero()) {
        return;
    }
    // One nominal period is at most a few milliseconds, so a plain sleep keeps stop
    // latency negligible while avoiding a busy wait on the acquisition thread.
    try {
        std::this_thread::sleep_for(remaining);
    } catch (...) {
        std::this_thread::yield();
    }
}

SampleReadResult PacedSampleSource::read_group(protocol::SampleGroup& group) noexcept {
    if (period_ns_ > std::chrono::nanoseconds::zero()) {
        const auto now = clock_.monotonic_raw_ns();
        if (next_due_ns_ == 0U) {
            next_due_ns_ = now;
        } else if (now < next_due_ns_) {
            sleeper_(std::chrono::nanoseconds(next_due_ns_ - now));
        }
        const auto current = clock_.monotonic_raw_ns();
        const auto scheduled = next_due_ns_ + static_cast<std::uint64_t>(period_ns_.count());
        // Keep the periodic grid when only slightly behind; restart one full period from
        // now after a stall so the source never bursts to catch up missed groups.
        next_due_ns_ = current >= scheduled ? current + static_cast<std::uint64_t>(period_ns_.count())
                                            : scheduled;
    }
    return inner_->read_group(group);
}

}  // namespace shakepie
