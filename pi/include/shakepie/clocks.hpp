#pragma once

#include <cstdint>

namespace shakepie {

struct ClockSnapshot {
    std::uint64_t realtime_ns{};
    std::uint64_t monotonic_raw_ns{};
};

class IClock {
public:
    virtual ~IClock() = default;
    [[nodiscard]] virtual std::uint64_t monotonic_raw_ns() noexcept = 0;
    [[nodiscard]] virtual std::uint64_t realtime_ns() noexcept = 0;
    [[nodiscard]] virtual ClockSnapshot snapshot() noexcept = 0;
};

class SystemClock final : public IClock {
public:
    [[nodiscard]] std::uint64_t monotonic_raw_ns() noexcept override;
    [[nodiscard]] std::uint64_t realtime_ns() noexcept override;
    [[nodiscard]] ClockSnapshot snapshot() noexcept override;
};

}  // namespace shakepie
