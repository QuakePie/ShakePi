#include "shakepie/ads1256.hpp"

#include "shakepie/protocol.hpp"

#include <array>
#include <cmath>
#include <utility>

namespace shakepie {
namespace {

constexpr std::uint8_t kRegStatus = 0x00;
constexpr std::uint8_t kRegMux = 0x01;
constexpr std::uint8_t kRegAdcon = 0x02;
constexpr std::uint8_t kRegDrate = 0x03;
constexpr std::uint8_t kCmdRdata = 0x01;
constexpr std::uint8_t kCmdRreg = 0x10;
constexpr std::uint8_t kCmdWreg = 0x50;
constexpr std::uint8_t kCmdSelfcal = 0xf0;
constexpr std::uint8_t kCmdSync = 0xfc;
constexpr std::uint8_t kCmdWakeup = 0x00;
constexpr std::array<std::uint8_t, 3> kMux{0x01, 0x23, 0x45};
constexpr auto kT6 = std::chrono::microseconds(7);  // >= 50 CLKIN periods at 7.68 MHz.
constexpr auto kT11 = std::chrono::microseconds(1); // >= 4 CLKIN periods at 7.68 MHz.

}  // namespace

Ads1256::Ads1256(AdcConfig config, IAds1256Transport& transport) noexcept
    : config_(std::move(config)), transport_(transport) {}

std::uint8_t Ads1256::gain_code(unsigned gain) noexcept {
    switch (gain) {
        case 1: return 0;
        case 2: return 1;
        case 4: return 2;
        case 8: return 3;
        case 16: return 4;
        case 32: return 5;
        case 64: return 6;
        default: return 0xff;
    }
}

std::uint8_t Ads1256::data_rate_code(double rate) noexcept {
    struct RateCode { double rate; std::uint8_t code; };
    static constexpr std::array<RateCode, 16> values{{
        {30000.0, 0xf0}, {15000.0, 0xe0}, {7500.0, 0xd0}, {3750.0, 0xc0},
        {2000.0, 0xb0}, {1000.0, 0xa1}, {500.0, 0x92}, {100.0, 0x82},
        {60.0, 0x72}, {50.0, 0x63}, {30.0, 0x53}, {25.0, 0x43},
        {15.0, 0x33}, {10.0, 0x23}, {5.0, 0x13}, {2.5, 0x03},
    }};
    for (const auto& value : values) {
        if (std::abs(rate - value.rate) < 0.001) return value.code;
    }
    return 0xff;
}

bool Ads1256::select(bool active) noexcept {
    return transport_.set_chip_select(!active);
}

bool Ads1256::transfer(std::uint8_t output, std::uint8_t* input) noexcept {
    std::uint8_t received = 0;
    if (!transport_.transfer_byte(output, received)) return false;
    if (input != nullptr) *input = received;
    return true;
}

bool Ads1256::command(std::uint8_t value) noexcept {
    if (!select(true)) return false;
    const bool result = transfer(value);
    return select(false) && result;
}

bool Ads1256::write_register(std::uint8_t reg, std::uint8_t value) noexcept {
    if (!select(true)) return false;
    const bool result = transfer(static_cast<std::uint8_t>(kCmdWreg | reg)) && transfer(0x00) && transfer(value);
    return select(false) && result;
}

bool Ads1256::read_register(std::uint8_t reg, std::uint8_t& value) noexcept {
    if (!select(true)) return false;
    bool result = transfer(static_cast<std::uint8_t>(kCmdRreg | reg)) && transfer(0x00);
    transport_.delay(kT6);
    result = result && transfer(0xff, &value);
    return select(false) && result;
}

bool Ads1256::configure(std::string& error) {
    const auto timeout = std::chrono::milliseconds(config_.drdy_timeout_ms);
    if (!transport_.wait_data_ready(timeout)) {
        error = "ADS1256 did not become ready before configuration";
        return false;
    }
    const std::uint8_t gain = gain_code(config_.pga_gain);
    const std::uint8_t rate = data_rate_code(config_.data_rate_sps);
    if (gain == 0xff || rate == 0xff) {
        error = "unsupported ADS1256 gain or data rate";
        return false;
    }
    const std::uint8_t status = config_.input_buffer ? 0x02U : 0x00U;
    if (!write_register(kRegStatus, status) || !write_register(kRegMux, kMux[0]) ||
        !write_register(kRegAdcon, gain) || !write_register(kRegDrate, rate) ||
        !command(kCmdSelfcal)) {
        error = "SPI failure while configuring ADS1256";
        return false;
    }
    transport_.delay(kT11);
    if (!transport_.wait_data_ready(timeout)) {
        error = "ADS1256 self-calibration timed out";
        return false;
    }
    return true;
}

bool Ads1256::prime(std::string& error) {
    if (!write_register(kRegMux, kMux[0]) || !command(kCmdSync)) {
        error = "failed to select initial ADS1256 channel";
        return false;
    }
    transport_.delay(kT11);
    if (!command(kCmdWakeup)) {
        error = "failed to wake ADS1256 while priming";
        return false;
    }
    transport_.delay(kT11);
    if (!transport_.wait_data_ready(std::chrono::milliseconds(config_.drdy_timeout_ms))) {
        error = "ADS1256 pipeline prime timed out";
        return false;
    }
    std::int32_t discarded = 0;
    if (!read_data(discarded)) {
        error = "failed to discard initial ADS1256 conversion";
        return false;
    }
    current_channel_ = 0;
    synchronized_ = true;
    return true;
}

bool Ads1256::initialize(std::string& error) {
    if (!transport_.set_chip_select(true) || !transport_.set_reset(true)) {
        error = "failed to initialize ADS1256 control lines";
        return false;
    }
    transport_.delay(std::chrono::milliseconds(200));
    if (!transport_.set_reset(false)) {
        error = "failed to assert ADS1256 reset";
        return false;
    }
    transport_.delay(std::chrono::milliseconds(10));
    if (!transport_.set_reset(true)) {
        error = "failed to release ADS1256 reset";
        return false;
    }
    transport_.delay(std::chrono::milliseconds(20));
    if (!transport_.wait_data_ready(std::chrono::milliseconds(config_.drdy_timeout_ms))) {
        error = "ADS1256 reset timed out";
        return false;
    }
    std::uint8_t status = 0;
    if (!read_register(kRegStatus, status)) {
        error = "failed to read ADS1256 chip ID";
        return false;
    }
    if ((status >> 4U) != 3U) {
        error = "unexpected ADS1256 chip ID " + std::to_string(status >> 4U);
        return false;
    }
    return configure(error) && prime(error);
}

bool Ads1256::read_data(std::int32_t& value) noexcept {
    if (!select(true)) return false;
    bool result = transfer(kCmdRdata);
    transport_.delay(kT6);
    std::uint8_t high = 0;
    std::uint8_t middle = 0;
    std::uint8_t low = 0;
    result = result && transfer(0xff, &high) && transfer(0xff, &middle) && transfer(0xff, &low);
    result = select(false) && result;
    if (!result) return false;
    value = sign_extend_24((static_cast<std::uint32_t>(high) << 16U) |
                           (static_cast<std::uint32_t>(middle) << 8U) |
                           static_cast<std::uint32_t>(low));
    return true;
}

Ads1256::ConversionResult Ads1256::read_conversion(std::int32_t& value) noexcept {
    // Classic multiplexed-scan pattern: wait for a settled conversion of the
    // pair selected by the previous call, shift it out, then advance the mux.
    // Two details matter on real silicon (verified against the chip with
    // pi/tools/adc_probe.cpp):
    //   1. Each SPI command runs in its own chip-select frame; issuing WREG,
    //      SYNC, WAKEUP, and RDATA inside one frame silently kept the mux on
    //      the first input pair (all three channels digitized AIN0-1).
    //   2. The first conversion after a mux change is still settling and must
    //      be discarded; keeping it left each channel smeared with the
    //      previous channel's tail (frozen 45-degree polarization).
    const auto timeout = std::chrono::milliseconds(config_.drdy_timeout_ms);
    if (!transport_.wait_data_ready(timeout)) {
        return ConversionResult::timeout;
    }
    if (!read_data(value)) {
        return ConversionResult::transport_fault;
    }
    const std::size_t next = (current_channel_ + 1U) % kMux.size();
    if (!write_register(kRegMux, kMux[next])) {
        return ConversionResult::transport_fault;
    }
    transport_.delay(kT11);
    if (!command(kCmdSync)) {
        return ConversionResult::transport_fault;
    }
    transport_.delay(kT11);
    if (!command(kCmdWakeup)) {
        return ConversionResult::transport_fault;
    }
    std::int32_t discarded = 0;
    if (!transport_.wait_data_ready(timeout)) {
        return ConversionResult::timeout;
    }
    if (!read_data(discarded)) {
        return ConversionResult::transport_fault;
    }
    current_channel_ = next;
    return ConversionResult::ok;
}

SampleReadResult Ads1256::read_group(protocol::SampleGroup& group) noexcept {
    if (!synchronized_) {
        return {SampleReadStatus::fault,
                static_cast<std::uint32_t>(protocol::StatusFlag::adc_fault_seen), 0U, 0U};
    }
    std::array<std::int32_t*, 3> outputs{&group.north_count, &group.east_count, &group.vertical_count};
    for (auto* output : outputs) {
        const auto result = read_conversion(*output);
        if (result == ConversionResult::timeout) {
            synchronized_ = false;
            return {SampleReadStatus::timeout,
                    static_cast<std::uint32_t>(protocol::StatusFlag::adc_timeout_seen), 0U, 1U};
        }
        if (result == ConversionResult::transport_fault) {
            synchronized_ = false;
            return {SampleReadStatus::fault,
                    static_cast<std::uint32_t>(protocol::StatusFlag::adc_fault_seen), 0U, 1U};
        }
    }
    return {};
}

}  // namespace shakepie
