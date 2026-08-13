#pragma once

#include "shakepie/config.hpp"
#include "shakepie/sample_source.hpp"

#include <chrono>
#include <cstdint>
#include <string>

namespace shakepie {

class IAds1256Transport {
public:
    virtual ~IAds1256Transport() = default;
    [[nodiscard]] virtual bool set_reset(bool high) noexcept = 0;
    [[nodiscard]] virtual bool set_chip_select(bool high) noexcept = 0;
    [[nodiscard]] virtual bool transfer_byte(std::uint8_t output, std::uint8_t& input) noexcept = 0;
    [[nodiscard]] virtual bool wait_data_ready(std::chrono::nanoseconds timeout) noexcept = 0;
    virtual void delay(std::chrono::nanoseconds duration) noexcept = 0;
};

class Ads1256 final : public ISampleSource {
public:
    Ads1256(AdcConfig config, IAds1256Transport& transport) noexcept;

    [[nodiscard]] bool initialize(std::string& error) override;
    [[nodiscard]] SampleReadResult read_group(protocol::SampleGroup& group) noexcept override;

    [[nodiscard]] static constexpr std::int32_t sign_extend_24(std::uint32_t raw) noexcept {
        raw &= 0x00ffffffU;
        return (raw & 0x00800000U) != 0U
                   ? static_cast<std::int32_t>(raw | 0xff000000U)
                   : static_cast<std::int32_t>(raw);
    }

    [[nodiscard]] static std::uint8_t gain_code(unsigned gain) noexcept;
    [[nodiscard]] static std::uint8_t data_rate_code(double samples_per_second) noexcept;

private:
    enum class ConversionResult { ok, timeout, transport_fault };

    [[nodiscard]] bool select(bool active) noexcept;
    [[nodiscard]] bool transfer(std::uint8_t output, std::uint8_t* input = nullptr) noexcept;
    [[nodiscard]] bool command(std::uint8_t value) noexcept;
    [[nodiscard]] bool write_register(std::uint8_t reg, std::uint8_t value) noexcept;
    [[nodiscard]] bool read_register(std::uint8_t reg, std::uint8_t& value) noexcept;
    [[nodiscard]] bool configure(std::string& error);
    [[nodiscard]] bool prime(std::string& error);
    [[nodiscard]] ConversionResult read_conversion(std::int32_t& value) noexcept;
    [[nodiscard]] bool read_data(std::int32_t& value) noexcept;

    AdcConfig config_;
    IAds1256Transport& transport_;
    std::size_t current_channel_{0};
    bool synchronized_{false};
};

}  // namespace shakepie
