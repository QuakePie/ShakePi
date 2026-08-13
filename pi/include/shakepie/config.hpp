#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace shakepie {

enum class AdcBackend { hardware, fake };

struct AdcConfig {
    AdcBackend backend{AdcBackend::hardware};
    std::string spi_device{"/dev/spidev0.0"};
    std::uint32_t spi_speed_hz{1'920'000};
    std::string gpio_chip{"/dev/gpiochip0"};
    unsigned cs_line{22};
    unsigned drdy_line{17};
    unsigned reset_line{18};
    unsigned pga_gain{64};
    double data_rate_sps{7500.0};
    std::uint32_t reference_voltage_uv{2'500'000};
    std::uint32_t drdy_timeout_ms{1000};
    bool input_buffer{false};
};

struct AcquisitionConfig {
    std::uint32_t samples_per_block{64};
    std::uint32_t nominal_group_rate_hz{750};
    std::uint32_t max_consecutive_adc_errors{3};
};

struct StorageConfig {
    std::filesystem::path hdd_mount{"/mnt/shakepie-data"};
    std::filesystem::path data_directory{"/mnt/shakepie-data/recordings"};
    std::uint32_t rotation_minutes{15};
    std::uint32_t sync_interval_seconds{5};
    std::uint32_t queue_seconds{300};
    std::size_t aggregate_bytes{static_cast<std::size_t>(128U) * 1024U};
    std::uint64_t preallocate_bytes{0};
    std::string station_id{"SHAKEPIE"};
};

struct StreamConfig {
    std::string bind_address{"127.0.0.1"};
    std::uint16_t port{8765};
    std::size_t live_queue_blocks{128};
    std::size_t client_queue_blocks{32};
    std::size_t max_clients{4};
    std::uint32_t keepalive_seconds{30};
    std::uint32_t write_timeout_seconds{15};
};

struct HealthConfig {
    std::filesystem::path status_file{"/run/shakepie/health.json"};
};

struct CalibrationConfig {
    bool enabled{true};
    std::uint32_t window_seconds{10};
    // Startup calibration: one measurement per recorder start (the offset is then kept
    // for the whole run), after startup_delay_seconds of electronics settling, over a
    // long window for a precise zero. Manual requests keep using window_seconds.
    bool auto_on_start{true};
    std::uint32_t startup_delay_seconds{30};
    std::uint32_t startup_window_seconds{300};
    std::int32_t max_offset_counts{4'000'000};
    // Both live under the recorder's systemd StateDirectory (/var/lib/shakepie):
    // writable by the recorder despite ProtectSystem=strict, group-writable for the
    // web interface, and persistent across reboots.
    std::filesystem::path request_file{"/var/lib/shakepie/calibrate.request"};
    std::filesystem::path state_file{"/var/lib/shakepie/zero_offsets.conf"};
    bool restore_on_start{true};
};

struct FakeConfig {
    std::uint32_t seed{0x00c0ffeeU};
    std::int32_t noise_amplitude{250};
    std::uint32_t impulse_every{0};
    std::uint32_t clip_every{0};
    std::uint32_t gap_every{0};
    std::uint32_t timeout_every{0};
    std::uint32_t jitter_ns{0};
};

struct RecorderConfig {
    AdcConfig adc{};
    AcquisitionConfig acquisition{};
    StorageConfig storage{};
    StreamConfig stream{};
    HealthConfig health{};
    CalibrationConfig calibration{};
    FakeConfig fake{};
};

struct ConfigResult {
    RecorderConfig config{};
    std::vector<std::string> errors{};

    [[nodiscard]] bool ok() const noexcept { return errors.empty(); }
};

[[nodiscard]] ConfigResult load_config(const std::filesystem::path& path);
[[nodiscard]] std::vector<std::string> validate_config(const RecorderConfig& config);

}  // namespace shakepie
