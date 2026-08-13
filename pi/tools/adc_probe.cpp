// Standalone ADS1256 diagnostic: verifies MUX switching and per-input independence.
//
// Built and run directly on the recorder while the recorder service is STOPPED
// (it needs exclusive SPI/GPIO access). Answers two questions the recorded data
// cannot separate:
//   1. Do WREG MUX writes actually stick? (read the register back)
//   2. Do the three input pairs (AIN0-1 / AIN2-3 / AIN4-5) see different signals?
//
// Usage: sudo ./shakepie-adc-probe [--config /etc/shakepie/recorder.conf]
// Exit 0 = mux switching works; exit 1 = a fault or a wiring-tie was detected.

#include "shakepie/ads1256.hpp"
#include "shakepie/config.hpp"
#include "shakepie/linux_hardware.hpp"

#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

using namespace std::chrono_literals;

constexpr std::uint8_t kRegMux = 0x01;
constexpr std::uint8_t kCmdRreg = 0x10;
constexpr std::uint8_t kCmdWreg = 0x50;
constexpr std::uint8_t kCmdRdata = 0x01;
constexpr std::uint8_t kCmdSync = 0xfc;
constexpr std::uint8_t kCmdWakeup = 0x00;
constexpr auto kT6 = std::chrono::microseconds(7);
constexpr auto kT11 = std::chrono::microseconds(1);

std::string trim(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return "";
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

// recorder.conf uses flat dotted keys ("adc.spi_device=..."), one per line.
shakepie::AdcConfig parse_config(const std::string& path) {
    shakepie::AdcConfig config{};
    std::ifstream input(path);
    std::string line;
    while (std::getline(input, line)) {
        const auto hash = line.find('#');
        if (hash != std::string::npos) line = line.substr(0, hash);
        const auto equals = line.find('=');
        if (equals == std::string::npos) continue;
        const std::string key = trim(line.substr(0, equals));
        const std::string value = trim(line.substr(equals + 1));
        if (key.rfind("adc.", 0) != 0) continue;
        if (key == "adc.spi_device") config.spi_device = value;
        else if (key == "adc.spi_speed_hz") config.spi_speed_hz = static_cast<std::uint32_t>(std::stoul(value));
        else if (key == "adc.gpio_chip") config.gpio_chip = value;
        else if (key == "adc.cs_line") config.cs_line = static_cast<unsigned>(std::stoul(value));
        else if (key == "adc.drdy_line") config.drdy_line = static_cast<unsigned>(std::stoul(value));
        else if (key == "adc.reset_line") config.reset_line = static_cast<unsigned>(std::stoul(value));
        else if (key == "adc.pga_gain") config.pga_gain = static_cast<unsigned>(std::stoul(value));
        else if (key == "adc.data_rate_sps") config.data_rate_sps = std::stod(value);
        else if (key == "adc.drdy_timeout_ms") config.drdy_timeout_ms = std::stoul(value);
        else if (key == "adc.input_buffer") config.input_buffer = value == "true" || value == "1";
    }
    return config;
}

class Probe {
public:
    explicit Probe(shakepie::IAds1256Transport& transport) : transport_(transport) {}

    bool select(bool active) { return transport_.set_chip_select(!active); }

    bool transfer(std::uint8_t output, std::uint8_t* input = nullptr) {
        std::uint8_t received = 0;
        if (!transport_.transfer_byte(output, received)) return false;
        if (input != nullptr) *input = received;
        return true;
    }

    bool write_register(std::uint8_t reg, std::uint8_t value) {
        if (!select(true)) return false;
        const bool ok = transfer(static_cast<std::uint8_t>(kCmdWreg | reg)) && transfer(0x00) &&
                        transfer(value);
        return select(false) && ok;
    }

    bool read_register(std::uint8_t reg, std::uint8_t& value) {
        if (!select(true)) return false;
        bool ok = transfer(static_cast<std::uint8_t>(kCmdRreg | reg)) && transfer(0x00);
        transport_.delay(kT6);
        ok = ok && transfer(0xff, &value);
        return select(false) && ok;
    }

    bool read_data(std::int32_t& value) {
        if (!select(true)) return false;
        bool ok = transfer(kCmdRdata);
        transport_.delay(kT6);
        std::uint8_t high = 0;
        std::uint8_t middle = 0;
        std::uint8_t low = 0;
        ok = ok && transfer(0xff, &high) && transfer(0xff, &middle) && transfer(0xff, &low);
        return select(false) && ok &&
               (value = shakepie::Ads1256::sign_extend_24(
                    (static_cast<std::uint32_t>(high) << 16U) |
                    (static_cast<std::uint32_t>(middle) << 8U) |
                    static_cast<std::uint32_t>(low)),
                true);
    }

    bool wait_ready(std::chrono::nanoseconds timeout) { return transport_.wait_data_ready(timeout); }

    bool command(std::uint8_t value) {
        if (!select(true)) return false;
        const bool ok = transfer(value);
        return select(false) && ok;
    }

private:
    shakepie::IAds1256Transport& transport_;
};

std::string describe(const std::vector<std::int32_t>& values) {
    std::int32_t minimum = values.front();
    std::int32_t maximum = values.front();
    for (const auto value : values) {
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }
    std::ostringstream text;
    text << "min=" << minimum << " max=" << maximum << " samples=[";
    for (std::size_t index = 0; index < values.size() && index < 4; ++index) {
        text << values[index] << (index + 1 < values.size() && index < 3 ? ", " : "");
    }
    text << "]";
    return text.str();
}

}  // namespace

int main(int argc, char** argv) {
    std::string config_path = "/etc/shakepie/recorder.conf";
    if (argc == 3 && std::string(argv[1]) == "--config") config_path = argv[2];
    const auto config = parse_config(config_path);

    std::string error;
    auto transport = shakepie::make_linux_ads1256_transport(config, error);
    if (!transport) {
        std::cerr << "transport open failed: " << error << "\n";
        return 1;
    }
    shakepie::Ads1256 adc(config, *transport);
    if (!adc.initialize(error)) {
        std::cerr << "ADS1256 initialize failed: " << error << "\n";
        return 1;
    }

    Probe probe(*transport);
    const auto timeout = std::chrono::milliseconds(config.drdy_timeout_ms);

    // Dump every configuration register a few times; a healthy chip answers with
    // stable values (STATUS carries the chip ID, MUX defaults to 0x01).
    for (const std::uint8_t reg : {0x00, 0x01, 0x02, 0x03}) {
        std::cout << "reg 0x" << std::hex << static_cast<int>(reg) << std::dec << ":";
        for (int attempt = 0; attempt < 3; ++attempt) {
            std::uint8_t value = 0;
            if (!probe.read_register(reg, value)) {
                std::cout << " SPI-FAULT";
                continue;
            }
            transport->delay(20us);
            std::cout << " 0x" << std::hex << static_cast<int>(value) << std::dec;
        }
        std::cout << "\n";
    }

    bool readback_ok = true;
    bool inputs_tied = true;
    std::vector<std::vector<std::int32_t>> per_pair;
    for (const std::uint8_t pair : {0x23, 0x45, 0x01}) {
        if (!probe.write_register(kRegMux, pair)) {
            std::cerr << "SPI failure writing MUX\n";
            return 1;
        }
        transport->delay(20us);
        std::uint8_t readback = 0;
        if (!probe.read_register(kRegMux, readback) || readback != pair) {
            std::cout << "pair 0x" << std::hex << static_cast<int>(pair) << std::dec
                      << ": READBACK FAILED (got 0x" << std::hex << static_cast<int>(readback)
                      << std::dec << ") -> register writes do not stick (software/driver issue)\n";
            readback_ok = false;
            continue;
        }
        // Restart conversions for the newly selected pair and discard the first
        // conversion (datasheet: first result after a mux change can be tainted).
        if (!probe.command(kCmdSync) || !probe.command(kCmdWakeup)) {
            std::cerr << "SPI failure sending SYNC/WAKEUP\n";
            return 1;
        }
        if (!probe.wait_ready(timeout)) {
            std::cerr << "DRDY timeout while settling pair 0x" << std::hex << static_cast<int>(pair)
                      << std::dec << "\n";
            return 1;
        }
        std::int32_t discarded = 0;
        if (!probe.read_data(discarded)) {
            std::cerr << "SPI failure reading data\n";
            return 1;
        }
        std::vector<std::int32_t> values;
        for (int index = 0; index < 8; ++index) {
            if (!probe.wait_ready(timeout)) {
                std::cerr << "DRDY timeout while sampling pair 0x" << std::hex
                          << static_cast<int>(pair) << std::dec << "\n";
                return 1;
            }
            std::int32_t value = 0;
            if (!probe.read_data(value)) {
                std::cerr << "SPI failure reading data\n";
                return 1;
            }
            values.push_back(value);
        }
        std::cout << "pair 0x" << std::hex << static_cast<int>(pair) << std::dec
                  << ": readback OK, " << describe(values) << "\n";
        per_pair.push_back(values);
    }

    if (!readback_ok) {
        std::cout << "VERDICT: MUX register writes do not stick - fix the driver/SPI path.\n";
        return 1;
    }
    if (per_pair.size() == 3) {
        // Inputs count as tied when every conversion of every pair is bit-identical
        // across the whole capture window (real independent inputs always differ).
        for (const auto& other : per_pair) {
            if (other != per_pair.front()) inputs_tied = false;
        }
        std::cout << (inputs_tied
                          ? "VERDICT: all three input pairs return identical values - the analog "
                            "inputs are electrically tied (check the geophone wiring at the "
                            "terminal block)."
                          : "VERDICT: MUX switching works and the input pairs are independent - "
                            "the ADC side is healthy.")
                  << "\n";
    }
    return inputs_tied ? 1 : 0;
}
