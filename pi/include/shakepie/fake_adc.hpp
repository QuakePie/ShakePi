#pragma once

#include "shakepie/clocks.hpp"
#include "shakepie/config.hpp"
#include "shakepie/sample_source.hpp"

#include <array>
#include <cstdint>

namespace shakepie {

class FakeAdc final : public ISampleSource {
public:
    FakeAdc(FakeConfig config, std::uint32_t group_rate_hz, IClock& clock);

    [[nodiscard]] bool initialize(std::string& error) override;
    [[nodiscard]] SampleReadResult read_group(protocol::SampleGroup& group) noexcept override;

private:
    [[nodiscard]] std::int32_t noise() noexcept;
    void wait_until_due() noexcept;

    FakeConfig config_;
    std::uint32_t group_rate_hz_;
    IClock& clock_;
    std::array<std::int32_t, 1024> sine_{};
    std::uint32_t random_state_;
    std::uint64_t group_index_{0};
    std::uint64_t next_due_ns_{0};
};

}  // namespace shakepie
