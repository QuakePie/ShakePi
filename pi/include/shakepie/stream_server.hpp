#pragma once

#if defined(__linux__)

#include "shakepie/config.hpp"
#include "shakepie/health.hpp"
#include "shakepie/queues.hpp"

#include <atomic>
#include <string>

namespace shakepie {

class StreamServer {
public:
    StreamServer(StreamConfig config,
                 LiveOverwriteRing& queue,
                 HealthCounters& health,
                 std::atomic<bool>& stop_requested,
                 std::atomic<bool>& acquisition_done);

    void run() noexcept;
    [[nodiscard]] const std::string& last_error() const noexcept { return last_error_; }

private:
    StreamConfig config_;
    LiveOverwriteRing& queue_;
    HealthCounters& health_;
    std::atomic<bool>& stop_requested_;
    std::atomic<bool>& acquisition_done_;
    std::string last_error_{};
};

}  // namespace shakepie

#endif
