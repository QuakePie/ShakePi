#include "test_harness.hpp"

#include "shakepie/ads1256.hpp"

#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace {

class MockTransport final : public shakepie::IAds1256Transport {
public:
    bool set_reset(bool high) noexcept override {
        reset_values.push_back(high);
        return true;
    }
    bool set_chip_select(bool high) noexcept override {
        cs_values.push_back(high);
        return true;
    }
    bool transfer_byte(std::uint8_t output, std::uint8_t& input) noexcept override {
        transfers.push_back(output);
        if (output == 0xff && read_index < read_values.size()) input = read_values[read_index++];
        else input = 0;
        return transfer_ok;
    }
    bool wait_data_ready(std::chrono::nanoseconds) noexcept override {
        ++waits;
        return ready;
    }
    void delay(std::chrono::nanoseconds duration) noexcept override { delays.push_back(duration); }

    std::vector<std::uint8_t> read_values{};
    std::vector<std::uint8_t> transfers{};
    std::vector<bool> reset_values{};
    std::vector<bool> cs_values{};
    std::vector<std::chrono::nanoseconds> delays{};
    std::size_t read_index{};
    std::size_t waits{};
    bool ready{true};
    bool transfer_ok{true};
};

}  // namespace

int main() {
    using shakepie::Ads1256;
    CHECK(Ads1256::sign_extend_24(0x000000U) == 0);
    CHECK(Ads1256::sign_extend_24(0x7fffffU) == 8'388'607);
    CHECK(Ads1256::sign_extend_24(0x800000U) == -8'388'608);
    CHECK(Ads1256::sign_extend_24(0xffffffU) == -1);
    CHECK(Ads1256::gain_code(64) == 6U);
    CHECK(Ads1256::gain_code(3) == 0xffU);
    CHECK(Ads1256::data_rate_code(7500.0) == 0xd0U);

    MockTransport transport;
    // Chip ID, discarded prime conversion, then N=1, E=2, Z=-1; each channel
    // switch also consumes one discarded settling conversion.
    transport.read_values = {
        0x30,
        0x00, 0x00, 0x00,
        0x00, 0x00, 0x01,
        0x00, 0x00, 0x00,
        0x00, 0x00, 0x02,
        0x00, 0x00, 0x00,
        0xff, 0xff, 0xff,
        0x00, 0x00, 0x00,
    };
    shakepie::AdcConfig config{};
    Ads1256 adc(config, transport);
    std::string error;
    CHECK(adc.initialize(error));
    CHECK(error.empty());
    CHECK((transport.reset_values == std::vector<bool>{true, false, true}));
    transport.transfers.clear();

    shakepie::protocol::SampleGroup group{};
    CHECK(adc.read_group(group).status == shakepie::SampleReadStatus::ok);
    CHECK(group.north_count == 1);
    CHECK(group.east_count == 2);
    CHECK(group.vertical_count == -1);
    // Scan pattern per channel: read the settled conversion, advance the mux in
    // its own chip-select frame (WREG, SYNC, WAKEUP), then read and discard the
    // first, still-settling conversion of the new pair.
    const std::vector<std::uint8_t> expected{
        0x01, 0xff, 0xff, 0xff, 0x51, 0x00, 0x23, 0xfc, 0x00, 0x01, 0xff, 0xff, 0xff,
        0x01, 0xff, 0xff, 0xff, 0x51, 0x00, 0x45, 0xfc, 0x00, 0x01, 0xff, 0xff, 0xff,
        0x01, 0xff, 0xff, 0xff, 0x51, 0x00, 0x01, 0xfc, 0x00, 0x01, 0xff, 0xff, 0xff,
    };
    CHECK(transport.transfers == expected);
    transport.ready = false;
    CHECK(adc.read_group(group).status == shakepie::SampleReadStatus::timeout);
    transport.ready = true;
    CHECK(adc.read_group(group).status == shakepie::SampleReadStatus::fault);

    MockTransport timeout;
    timeout.ready = false;
    Ads1256 failed(config, timeout);
    CHECK(!failed.initialize(error));
    CHECK(!error.empty());
    return shakepie_test_failures == 0 ? 0 : 1;
}
