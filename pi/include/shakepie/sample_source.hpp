#pragma once

#include "shakepie/protocol.hpp"

#include <cstdint>
#include <string>

namespace shakepie {

enum class SampleReadStatus { ok, timeout, fault };

struct SampleReadResult {
    SampleReadStatus status{SampleReadStatus::ok};
    std::uint32_t status_flags{};
    std::uint32_t timing_flags{};
    std::uint32_t sequence_advance{1};
};

class ISampleSource {
public:
    virtual ~ISampleSource() = default;
    [[nodiscard]] virtual bool initialize(std::string& error) = 0;
    [[nodiscard]] virtual SampleReadResult read_group(protocol::SampleGroup& group) noexcept = 0;
};

}  // namespace shakepie
