#include "shakepie/clocks.hpp"

#include <chrono>

#if defined(__linux__)
#include <time.h>
#endif

namespace shakepie {
namespace {

#if defined(__linux__)
std::uint64_t clock_ns(clockid_t id) noexcept {
    timespec value{};
    if (::clock_gettime(id, &value) != 0) {
        return 0;
    }
    return static_cast<std::uint64_t>(value.tv_sec) * 1'000'000'000ULL +
           static_cast<std::uint64_t>(value.tv_nsec);
}
#endif

}  // namespace

std::uint64_t SystemClock::monotonic_raw_ns() noexcept {
#if defined(__linux__)
    return clock_ns(CLOCK_MONOTONIC_RAW);
#else
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
#endif
}

std::uint64_t SystemClock::realtime_ns() noexcept {
#if defined(__linux__)
    return clock_ns(CLOCK_REALTIME);
#else
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
#endif
}

ClockSnapshot SystemClock::snapshot() noexcept {
    // The midpoint monotonic timestamp bounds mapping error to the two realtime calls.
    const auto real_before = realtime_ns();
    const auto monotonic = monotonic_raw_ns();
    const auto real_after = realtime_ns();
    const auto midpoint = real_after >= real_before
                              ? real_before + ((real_after - real_before) / 2U)
                              : real_after + ((real_before - real_after) / 2U);
    return ClockSnapshot{midpoint, monotonic};
}

}  // namespace shakepie
