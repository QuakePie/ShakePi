#include "shakepie/config.hpp"

#include "shakepie/protocol.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cctype>
#include <fstream>
#include <limits>
#include <string_view>
#include <unordered_map>

namespace shakepie {
namespace {

std::string trim(std::string value) {
    const auto not_space = [](unsigned char character) { return !std::isspace(character); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

template <typename T>
bool parse_unsigned(std::string_view text, T& output) {
    T value{};
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size()) {
        return false;
    }
    output = value;
    return true;
}

bool parse_signed(std::string_view text, std::int32_t& output) {
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), output);
    return error == std::errc{} && end == text.data() + text.size();
}

bool parse_bool(std::string_view text, bool& output) {
    if (text == "true") {
        output = true;
        return true;
    }
    if (text == "false") {
        output = false;
        return true;
    }
    return false;
}

bool parse_rate(std::string_view text, double& output) {
    static constexpr std::array<std::pair<std::string_view, double>, 16> rates{{
        {"30000", 30000.0}, {"15000", 15000.0}, {"7500", 7500.0}, {"3750", 3750.0},
        {"2000", 2000.0}, {"1000", 1000.0}, {"500", 500.0}, {"100", 100.0},
        {"60", 60.0}, {"50", 50.0}, {"30", 30.0}, {"25", 25.0}, {"15", 15.0},
        {"10", 10.0}, {"5", 5.0}, {"2.5", 2.5},
    }};
    const auto match = std::find_if(rates.begin(), rates.end(), [text](const auto& item) {
        return item.first == text;
    });
    if (match == rates.end()) {
        return false;
    }
    output = match->second;
    return true;
}

}  // namespace

ConfigResult load_config(const std::filesystem::path& path) {
    ConfigResult result{};
    std::ifstream input(path);
    if (!input) {
        result.errors.push_back("cannot open configuration: " + path.string());
        return result;
    }

    std::unordered_map<std::string, std::string> values;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        const auto comment = line.find('#');
        if (comment != std::string::npos) {
            line.erase(comment);
        }
        line = trim(std::move(line));
        if (line.empty()) {
            continue;
        }
        const auto equals = line.find('=');
        if (equals == std::string::npos) {
            result.errors.push_back("line " + std::to_string(line_number) + ": expected key=value");
            continue;
        }
        std::string key = trim(line.substr(0, equals));
        std::string value = trim(line.substr(equals + 1));
        if (key.empty() || value.empty() || values.contains(key)) {
            result.errors.push_back("line " + std::to_string(line_number) + ": empty or duplicate setting");
            continue;
        }
        values.emplace(std::move(key), std::move(value));
    }

    auto take_string = [&](const char* key, std::string& target) {
        if (const auto iterator = values.find(key); iterator != values.end()) {
            target = iterator->second;
            values.erase(iterator);
        }
    };
    auto take_path = [&](const char* key, std::filesystem::path& target) {
        if (const auto iterator = values.find(key); iterator != values.end()) {
            target = iterator->second;
            values.erase(iterator);
        }
    };
    auto take_unsigned = [&](const char* key, auto& target) {
        if (const auto iterator = values.find(key); iterator != values.end()) {
            if (!parse_unsigned(iterator->second, target)) {
                result.errors.push_back(std::string(key) + ": invalid unsigned integer");
            }
            values.erase(iterator);
        }
    };
    auto take_bool = [&](const char* key, bool& target) {
        if (const auto iterator = values.find(key); iterator != values.end()) {
            if (!parse_bool(iterator->second, target)) {
                result.errors.push_back(std::string(key) + ": expected true or false");
            }
            values.erase(iterator);
        }
    };

    if (const auto iterator = values.find("adc.backend"); iterator != values.end()) {
        if (iterator->second == "hardware") result.config.adc.backend = AdcBackend::hardware;
        else if (iterator->second == "fake") result.config.adc.backend = AdcBackend::fake;
        else result.errors.push_back("adc.backend: expected hardware or fake");
        values.erase(iterator);
    }
    take_string("adc.spi_device", result.config.adc.spi_device);
    take_unsigned("adc.spi_speed_hz", result.config.adc.spi_speed_hz);
    take_string("adc.gpio_chip", result.config.adc.gpio_chip);
    take_unsigned("adc.cs_line", result.config.adc.cs_line);
    take_unsigned("adc.drdy_line", result.config.adc.drdy_line);
    take_unsigned("adc.reset_line", result.config.adc.reset_line);
    take_unsigned("adc.pga_gain", result.config.adc.pga_gain);
    if (const auto iterator = values.find("adc.data_rate_sps"); iterator != values.end()) {
        if (!parse_rate(iterator->second, result.config.adc.data_rate_sps)) {
            result.errors.push_back("adc.data_rate_sps: unsupported ADS1256 rate");
        }
        values.erase(iterator);
    }
    take_unsigned("adc.reference_voltage_uv", result.config.adc.reference_voltage_uv);
    take_unsigned("adc.drdy_timeout_ms", result.config.adc.drdy_timeout_ms);
    take_bool("adc.input_buffer", result.config.adc.input_buffer);
    take_unsigned("acquisition.samples_per_block", result.config.acquisition.samples_per_block);
    take_unsigned("acquisition.nominal_group_rate_hz", result.config.acquisition.nominal_group_rate_hz);
    take_unsigned("acquisition.max_consecutive_adc_errors", result.config.acquisition.max_consecutive_adc_errors);
    take_path("storage.hdd_mount", result.config.storage.hdd_mount);
    take_path("storage.data_directory", result.config.storage.data_directory);
    take_unsigned("storage.rotation_minutes", result.config.storage.rotation_minutes);
    take_unsigned("storage.sync_interval_seconds", result.config.storage.sync_interval_seconds);
    take_unsigned("storage.queue_seconds", result.config.storage.queue_seconds);
    std::uint64_t aggregate_kib = result.config.storage.aggregate_bytes / 1024U;
    take_unsigned("storage.aggregate_kib", aggregate_kib);
    if (aggregate_kib > std::numeric_limits<std::size_t>::max() / 1024U) {
        result.config.storage.aggregate_bytes = std::numeric_limits<std::size_t>::max();
    } else {
        result.config.storage.aggregate_bytes = static_cast<std::size_t>(aggregate_kib * 1024U);
    }
    std::uint64_t preallocate_mib = 0;
    take_unsigned("storage.preallocate_mib", preallocate_mib);
    constexpr std::uint64_t bytes_per_mib = 1024ULL * 1024ULL;
    if (preallocate_mib > std::numeric_limits<std::uint64_t>::max() / bytes_per_mib) {
        result.config.storage.preallocate_bytes = std::numeric_limits<std::uint64_t>::max();
    } else {
        result.config.storage.preallocate_bytes = preallocate_mib * bytes_per_mib;
    }
    take_string("storage.station_id", result.config.storage.station_id);
    take_string("stream.bind_address", result.config.stream.bind_address);
    take_unsigned("stream.port", result.config.stream.port);
    take_unsigned("stream.live_queue_blocks", result.config.stream.live_queue_blocks);
    take_unsigned("stream.client_queue_blocks", result.config.stream.client_queue_blocks);
    take_unsigned("stream.max_clients", result.config.stream.max_clients);
    take_unsigned("stream.keepalive_seconds", result.config.stream.keepalive_seconds);
    take_unsigned("stream.write_timeout_seconds", result.config.stream.write_timeout_seconds);
    take_path("health.status_file", result.config.health.status_file);
    take_bool("calibration.enabled", result.config.calibration.enabled);
    take_unsigned("calibration.window_seconds", result.config.calibration.window_seconds);
    take_bool("calibration.auto_on_start", result.config.calibration.auto_on_start);
    take_unsigned("calibration.startup_delay_seconds", result.config.calibration.startup_delay_seconds);
    take_unsigned("calibration.startup_window_seconds", result.config.calibration.startup_window_seconds);
    if (const auto iterator = values.find("calibration.max_offset_counts");
        iterator != values.end()) {
        if (!parse_signed(iterator->second, result.config.calibration.max_offset_counts)) {
            result.errors.push_back("calibration.max_offset_counts: invalid integer");
        }
        values.erase(iterator);
    }
    take_path("calibration.request_file", result.config.calibration.request_file);
    take_path("calibration.state_file", result.config.calibration.state_file);
    take_bool("calibration.restore_on_start", result.config.calibration.restore_on_start);
    take_unsigned("fake.seed", result.config.fake.seed);
    if (const auto iterator = values.find("fake.noise_amplitude"); iterator != values.end()) {
        if (!parse_signed(iterator->second, result.config.fake.noise_amplitude)) {
            result.errors.push_back("fake.noise_amplitude: invalid integer");
        }
        values.erase(iterator);
    }
    take_unsigned("fake.impulse_every", result.config.fake.impulse_every);
    take_unsigned("fake.clip_every", result.config.fake.clip_every);
    take_unsigned("fake.gap_every", result.config.fake.gap_every);
    take_unsigned("fake.timeout_every", result.config.fake.timeout_every);
    take_unsigned("fake.jitter_ns", result.config.fake.jitter_ns);

    for (const auto& [key, unused] : values) {
        static_cast<void>(unused);
        result.errors.push_back("unknown setting: " + key);
    }
    auto validation = validate_config(result.config);
    result.errors.insert(result.errors.end(), validation.begin(), validation.end());
    return result;
}

std::vector<std::string> validate_config(const RecorderConfig& config) {
    std::vector<std::string> errors;
    static constexpr std::array<unsigned, 7> gains{1, 2, 4, 8, 16, 32, 64};
    if (std::find(gains.begin(), gains.end(), config.adc.pga_gain) == gains.end()) {
        errors.emplace_back("adc.pga_gain must be one of 1,2,4,8,16,32,64");
    }
    if (config.adc.reference_voltage_uv == 0U) errors.emplace_back("adc.reference_voltage_uv must be positive");
    if (config.adc.spi_device.empty() || config.adc.gpio_chip.empty()) {
        errors.emplace_back("ADC device paths cannot be empty");
    }
    if (config.adc.spi_speed_hz == 0U || config.adc.spi_speed_hz > 1'920'000U) {
        errors.emplace_back("adc.spi_speed_hz must be in 1..1920000 for the 7.68 MHz board clock");
    }
    if (config.adc.cs_line == config.adc.drdy_line || config.adc.cs_line == config.adc.reset_line ||
        config.adc.drdy_line == config.adc.reset_line) {
        errors.emplace_back("ADC CS, DRDY, and RESET GPIO lines must be distinct");
    }
    if (config.adc.drdy_timeout_ms == 0U) errors.emplace_back("adc.drdy_timeout_ms must be positive");
    if (config.acquisition.samples_per_block == 0U ||
        config.acquisition.samples_per_block > protocol::kMaxSamplesPerBlock) {
        errors.emplace_back("acquisition.samples_per_block must be between 1 and 256");
    }
    if (config.acquisition.nominal_group_rate_hz == 0U ||
        config.acquisition.nominal_group_rate_hz > 1'000'000U) {
        errors.emplace_back("nominal group rate must be in 1..1000000 Hz");
    }
    // Paced hardware acquisition runs a three-channel MUX cycle per group; each
    // channel switch also discards one settling conversion, so a group needs up
    // to six conversion periods and the configured ADC data rate must sustain it.
    if (config.adc.backend == AdcBackend::hardware &&
        config.acquisition.nominal_group_rate_hz != 0U &&
        static_cast<double>(config.acquisition.nominal_group_rate_hz) * 6.0 >=
            config.adc.data_rate_sps) {
        errors.emplace_back(
            "adc.data_rate_sps must exceed six times acquisition.nominal_group_rate_hz "
            "because each sample group requires three conversions plus one discarded "
            "settling conversion per channel switch");
    }
    if (config.acquisition.nominal_group_rate_hz != 0U &&
        static_cast<std::uint64_t>(config.acquisition.samples_per_block) *
                1'000'000'000ULL /
                config.acquisition.nominal_group_rate_hz >
            std::numeric_limits<std::uint32_t>::max()) {
        errors.emplace_back("samples per block and group rate exceed the uint32 timestamp span");
    }
    if (config.acquisition.max_consecutive_adc_errors == 0U) errors.emplace_back("max ADC errors must be positive");
    if (config.storage.hdd_mount.empty() || !config.storage.hdd_mount.is_absolute()) {
        errors.emplace_back("storage.hdd_mount must be an absolute path");
    }
    if (config.storage.data_directory.empty() || !config.storage.data_directory.is_absolute()) {
        errors.emplace_back("storage.data_directory must be an absolute path");
    }
    const auto relative = config.storage.data_directory.lexically_relative(config.storage.hdd_mount);
    if (relative.empty() || *relative.begin() == std::filesystem::path("..")) {
        errors.emplace_back("storage.data_directory must be inside storage.hdd_mount");
    }
    if (config.storage.rotation_minutes == 0U || config.storage.rotation_minutes > 24U * 60U) {
        errors.emplace_back("storage.rotation_minutes must be in 1..1440");
    }
    if (config.storage.sync_interval_seconds == 0U || config.storage.sync_interval_seconds > 300U) {
        errors.emplace_back("storage.sync_interval_seconds must be in 1..300");
    }
    if (config.storage.queue_seconds < 300U) errors.emplace_back("storage.queue_seconds must be at least 300");
    constexpr std::size_t minimum_aggregate_bytes = static_cast<std::size_t>(64U) * 1024U;
    constexpr std::size_t maximum_aggregate_bytes = static_cast<std::size_t>(256U) * 1024U;
    if (config.storage.aggregate_bytes < minimum_aggregate_bytes ||
        config.storage.aggregate_bytes > maximum_aggregate_bytes) {
        errors.emplace_back("storage.aggregate_kib must be in 64..256");
    }
    if (config.storage.preallocate_bytes >
        static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
        errors.emplace_back("storage.preallocate_mib exceeds the supported file size");
    }
    if (config.storage.station_id.empty() || config.storage.station_id.size() > 31U) {
        errors.emplace_back("storage.station_id must contain 1..31 bytes");
    } else if (!std::all_of(config.storage.station_id.begin(), config.storage.station_id.end(), [](unsigned char c) {
                   return std::isalnum(c) || c == '-' || c == '_';
               })) {
        errors.emplace_back("storage.station_id may contain only letters, digits, '-' and '_'");
    }
    const bool loopback = config.stream.bind_address == "127.0.0.1" || config.stream.bind_address == "::1";
    if (!loopback) errors.emplace_back("stream.bind_address must be a loopback address");
    if (config.stream.port == 0U) errors.emplace_back("stream.port must be nonzero");
    if (config.stream.live_queue_blocks < 2U || config.stream.live_queue_blocks > 65'536U) {
        errors.emplace_back("stream.live_queue_blocks must be in 2..65536");
    }
    if (config.stream.client_queue_blocks < 2U || config.stream.client_queue_blocks > 4'096U) {
        errors.emplace_back("stream.client_queue_blocks must be in 2..4096");
    }
    if (config.stream.max_clients == 0U || config.stream.max_clients > 64U) errors.emplace_back("max clients must be 1..64");
    if (config.stream.keepalive_seconds == 0U || config.stream.keepalive_seconds > 86'400U ||
        config.stream.write_timeout_seconds == 0U ||
        config.stream.write_timeout_seconds > 86'400U) {
        errors.emplace_back("stream keepalive and write timeout must be in 1..86400 seconds");
    }
    if (config.health.status_file.empty() || config.health.status_file.string().front() != '/') {
        errors.emplace_back("health.status_file must be an absolute Linux path");
    }
    if (config.fake.noise_amplitude < 0) errors.emplace_back("fake.noise_amplitude cannot be negative");
    if (config.fake.jitter_ns > 100'000'000U) errors.emplace_back("fake.jitter_ns must not exceed 100 ms");
    if (config.calibration.window_seconds == 0U || config.calibration.window_seconds > 120U) {
        errors.emplace_back("calibration.window_seconds must be in 1..120");
    }
    if (config.calibration.startup_delay_seconds > 3'600U) {
        errors.emplace_back("calibration.startup_delay_seconds must be in 0..3600");
    }
    if (config.calibration.startup_window_seconds < 30U ||
        config.calibration.startup_window_seconds > 3'600U) {
        errors.emplace_back("calibration.startup_window_seconds must be in 30..3600");
    }
    if (config.calibration.max_offset_counts <= 0 ||
        config.calibration.max_offset_counts > 8'388'607) {
        errors.emplace_back("calibration.max_offset_counts must be in 1..8388607");
    }
    if (config.calibration.request_file.empty() || config.calibration.state_file.empty()) {
        errors.emplace_back("calibration request and state files must not be empty");
    }
    return errors;
}

}  // namespace shakepie
