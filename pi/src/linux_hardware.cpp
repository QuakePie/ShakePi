#include "shakepie/linux_hardware.hpp"

#if defined(__linux__)

#include <gpiod.h>
#include <linux/spi/spidev.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <limits>
#include <memory>
#include <string>
#include <system_error>

#include <fcntl.h>

namespace shakepie {
namespace {

std::string errno_message(int error_number) {
    return std::error_code(error_number == 0 ? EIO : error_number, std::generic_category()).message();
}

std::uint64_t monotonic_ns() noexcept {
    timespec value{};
    if (::clock_gettime(CLOCK_MONOTONIC, &value) != 0) return 0;
    return static_cast<std::uint64_t>(value.tv_sec) * 1'000'000'000ULL +
           static_cast<std::uint64_t>(value.tv_nsec);
}

class SpiDevice {
public:
    // Descriptor and speed have distinct units despite being convertible integer types.
    SpiDevice(int descriptor, std::uint32_t speed) noexcept  // NOLINT(bugprone-easily-swappable-parameters)
        : descriptor_(descriptor), speed_(speed) {}
    ~SpiDevice() { if (descriptor_ >= 0) ::close(descriptor_); }
    SpiDevice(const SpiDevice&) = delete;
    SpiDevice& operator=(const SpiDevice&) = delete;

    bool transfer(std::uint8_t output, std::uint8_t& input) noexcept {
        spi_ioc_transfer transfer{};
        transfer.tx_buf = reinterpret_cast<std::uintptr_t>(&output);
        transfer.rx_buf = reinterpret_cast<std::uintptr_t>(&input);
        transfer.len = 1;
        transfer.speed_hz = speed_;
        transfer.bits_per_word = 8;
        return ::ioctl(descriptor_, SPI_IOC_MESSAGE(1), &transfer) == 1;
    }

private:
    int descriptor_;
    std::uint32_t speed_;
};

#if defined(SHAKEPIE_GPIOD_V2)

class LinuxAds1256Transport final : public IAds1256Transport {
public:
    LinuxAds1256Transport(int spi_fd,
                          std::uint32_t speed,
                          gpiod_line_request* request,
                          gpiod_edge_event_buffer* events,
                          unsigned cs,
                          unsigned reset,
                          unsigned drdy) noexcept
        : spi_(spi_fd, speed), request_(request), events_(events), cs_(cs), reset_(reset), drdy_(drdy) {}

    ~LinuxAds1256Transport() override {
        if (events_ != nullptr) gpiod_edge_event_buffer_free(events_);
        if (request_ != nullptr) gpiod_line_request_release(request_);
    }

    bool set_reset(bool high) noexcept override {
        return gpiod_line_request_set_value(
                   request_, reset_, high ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE) == 0;
    }
    bool set_chip_select(bool high) noexcept override {
        return gpiod_line_request_set_value(
                   request_, cs_, high ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE) == 0;
    }
    bool transfer_byte(std::uint8_t output, std::uint8_t& input) noexcept override {
        return spi_.transfer(output, input);
    }

    bool wait_data_ready(std::chrono::nanoseconds timeout) noexcept override {
        if (gpiod_line_request_get_value(request_, drdy_) == GPIOD_LINE_VALUE_INACTIVE) return true;
        const auto start = monotonic_ns();
        if (start == 0U) return false;
        const auto duration = static_cast<std::uint64_t>(timeout.count());
        const auto maximum = std::numeric_limits<std::uint64_t>::max();
        const auto deadline = start > maximum - duration ? maximum : start + duration;
        for (;;) {
            const auto now = monotonic_ns();
            if (now == 0U || now >= deadline) return false;
            const auto remaining = static_cast<std::int64_t>(deadline - now);
            const int ready = gpiod_line_request_wait_edge_events(request_, remaining);
            if (ready == 0) return false;
            if (ready < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            const int count = gpiod_line_request_read_edge_events(request_, events_, 4);
            if (count < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            for (int index = 0; index < count; ++index) {
                auto* event = gpiod_edge_event_buffer_get_event(events_, static_cast<unsigned long>(index));
                if (event != nullptr && gpiod_edge_event_get_line_offset(event) == drdy_ &&
                    gpiod_edge_event_get_event_type(event) == GPIOD_EDGE_EVENT_FALLING_EDGE) return true;
            }
        }
    }

    void delay(std::chrono::nanoseconds duration) noexcept override {
        timespec request{};
        request.tv_sec = static_cast<time_t>(duration.count() / 1'000'000'000LL);
        request.tv_nsec = static_cast<long>(duration.count() % 1'000'000'000LL);
        while (::clock_nanosleep(CLOCK_MONOTONIC, 0, &request, &request) == EINTR) {}
    }

private:
    SpiDevice spi_;
    gpiod_line_request* request_;
    gpiod_edge_event_buffer* events_;
    unsigned cs_;
    unsigned reset_;
    unsigned drdy_;
};

gpiod_line_request* request_gpio_lines(const AdcConfig& config) noexcept {
    gpiod_chip* chip = gpiod_chip_open(config.gpio_chip.c_str());
    if (chip == nullptr) return nullptr;
    gpiod_line_settings* output = gpiod_line_settings_new();
    gpiod_line_settings* input = gpiod_line_settings_new();
    gpiod_line_config* lines = gpiod_line_config_new();
    gpiod_request_config* request_config = gpiod_request_config_new();
    gpiod_line_request* request = nullptr;
    const unsigned outputs[] = {config.cs_line, config.reset_line};
    const unsigned input_offset = config.drdy_line;
    if (output != nullptr && input != nullptr && lines != nullptr && request_config != nullptr &&
        gpiod_line_settings_set_direction(output, GPIOD_LINE_DIRECTION_OUTPUT) == 0 &&
        gpiod_line_settings_set_output_value(output, GPIOD_LINE_VALUE_ACTIVE) == 0 &&
        gpiod_line_settings_set_direction(input, GPIOD_LINE_DIRECTION_INPUT) == 0 &&
        gpiod_line_settings_set_edge_detection(input, GPIOD_LINE_EDGE_FALLING) == 0 &&
        gpiod_line_config_add_line_settings(lines, outputs, 2, output) == 0 &&
        gpiod_line_config_add_line_settings(lines, &input_offset, 1, input) == 0) {
        gpiod_request_config_set_consumer(request_config, "shakepie-ads1256");
        gpiod_request_config_set_event_buffer_size(request_config, 16);
        request = gpiod_chip_request_lines(chip, request_config, lines);
    }
    if (request_config != nullptr) gpiod_request_config_free(request_config);
    if (lines != nullptr) gpiod_line_config_free(lines);
    if (input != nullptr) gpiod_line_settings_free(input);
    if (output != nullptr) gpiod_line_settings_free(output);
    gpiod_chip_close(chip);
    return request;
}

#else

class LinuxAds1256Transport final : public IAds1256Transport {
public:
    LinuxAds1256Transport(int spi_fd,
                          std::uint32_t speed,
                          gpiod_chip* chip,
                          gpiod_line* cs,  // NOLINT(bugprone-easily-swappable-parameters)
                          gpiod_line* reset,
                          gpiod_line* drdy) noexcept
        : spi_(spi_fd, speed), chip_(chip), cs_(cs), reset_(reset), drdy_(drdy) {}

    ~LinuxAds1256Transport() override {
        if (drdy_ != nullptr) gpiod_line_release(drdy_);
        if (reset_ != nullptr) gpiod_line_release(reset_);
        if (cs_ != nullptr) gpiod_line_release(cs_);
        if (chip_ != nullptr) gpiod_chip_close(chip_);
    }

    bool set_reset(bool high) noexcept override { return gpiod_line_set_value(reset_, high ? 1 : 0) == 0; }
    bool set_chip_select(bool high) noexcept override { return gpiod_line_set_value(cs_, high ? 1 : 0) == 0; }
    bool transfer_byte(std::uint8_t output, std::uint8_t& input) noexcept override {
        return spi_.transfer(output, input);
    }

    bool wait_data_ready(std::chrono::nanoseconds timeout) noexcept override {
        if (gpiod_line_get_value(drdy_) == 0) return true;
        const auto start = monotonic_ns();
        if (start == 0U) return false;
        const auto duration = static_cast<std::uint64_t>(timeout.count());
        const auto maximum = std::numeric_limits<std::uint64_t>::max();
        const auto deadline = start > maximum - duration ? maximum : start + duration;
        for (;;) {
            const auto now = monotonic_ns();
            if (now == 0U || now >= deadline) return false;
            const auto remaining = deadline - now;
            timespec wait{};
            wait.tv_sec = static_cast<time_t>(remaining / 1'000'000'000ULL);
            wait.tv_nsec = static_cast<long>(remaining % 1'000'000'000ULL);
            const int ready = gpiod_line_event_wait(drdy_, &wait);
            if (ready == 0) return false;
            if (ready < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            gpiod_line_event event{};
            if (gpiod_line_event_read(drdy_, &event) == 0 &&
                event.event_type == GPIOD_LINE_EVENT_FALLING_EDGE) return true;
        }
    }

    void delay(std::chrono::nanoseconds duration) noexcept override {
        timespec request{};
        request.tv_sec = static_cast<time_t>(duration.count() / 1'000'000'000LL);
        request.tv_nsec = static_cast<long>(duration.count() % 1'000'000'000LL);
        while (::clock_nanosleep(CLOCK_MONOTONIC, 0, &request, &request) == EINTR) {}
    }

private:
    SpiDevice spi_;
    gpiod_chip* chip_;
    gpiod_line* cs_;
    gpiod_line* reset_;
    gpiod_line* drdy_;
};

#endif

int open_spi(const AdcConfig& config, std::string& error) {
    const int descriptor = ::open(config.spi_device.c_str(), O_RDWR | O_CLOEXEC);
    if (descriptor < 0) {
        error = "cannot open " + config.spi_device + ": " + errno_message(errno);
        return -1;
    }
    std::uint8_t mode = static_cast<std::uint8_t>(SPI_MODE_1 | SPI_NO_CS);
    std::uint8_t bits = 8;
    std::uint32_t speed = config.spi_speed_hz;
    if (::ioctl(descriptor, SPI_IOC_WR_MODE, &mode) < 0 ||
        ::ioctl(descriptor, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ::ioctl(descriptor, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0) {
        error = "cannot configure spidev: " + errno_message(errno);
        ::close(descriptor);
        return -1;
    }
    return descriptor;
}

}  // namespace

std::unique_ptr<IAds1256Transport> make_linux_ads1256_transport(const AdcConfig& config,
                                                               std::string& error) {
    const int spi_fd = open_spi(config, error);
    if (spi_fd < 0) return {};

#if defined(SHAKEPIE_GPIOD_V2)
    gpiod_line_request* request = request_gpio_lines(config);
    gpiod_edge_event_buffer* events = request != nullptr ? gpiod_edge_event_buffer_new(4) : nullptr;
    if (request == nullptr || events == nullptr) {
        error = "cannot request ADS1256 GPIO lines: " + errno_message(errno);
        if (events != nullptr) gpiod_edge_event_buffer_free(events);
        if (request != nullptr) gpiod_line_request_release(request);
        ::close(spi_fd);
        return {};
    }
    return std::make_unique<LinuxAds1256Transport>(spi_fd, config.spi_speed_hz, request, events,
                                                   config.cs_line, config.reset_line, config.drdy_line);
#else
    gpiod_chip* chip = gpiod_chip_open(config.gpio_chip.c_str());
    if (chip == nullptr) {
        error = "cannot open GPIO chip " + config.gpio_chip;
        ::close(spi_fd);
        return {};
    }
    gpiod_line* cs = gpiod_chip_get_line(chip, config.cs_line);
    gpiod_line* reset = gpiod_chip_get_line(chip, config.reset_line);
    gpiod_line* drdy = gpiod_chip_get_line(chip, config.drdy_line);
    bool cs_requested = false;
    bool reset_requested = false;
    bool drdy_requested = false;
    if (cs != nullptr) cs_requested = gpiod_line_request_output(cs, "shakepie-cs", 1) == 0;
    if (reset != nullptr) reset_requested = gpiod_line_request_output(reset, "shakepie-reset", 1) == 0;
    if (drdy != nullptr) drdy_requested = gpiod_line_request_falling_edge_events(drdy, "shakepie-drdy") == 0;
    if (!cs_requested || !reset_requested || !drdy_requested) {
        error = "cannot request ADS1256 GPIO lines: " + errno_message(errno);
        if (drdy_requested) gpiod_line_release(drdy);
        if (reset_requested) gpiod_line_release(reset);
        if (cs_requested) gpiod_line_release(cs);
        gpiod_chip_close(chip);
        ::close(spi_fd);
        return {};
    }
    return std::make_unique<LinuxAds1256Transport>(spi_fd, config.spi_speed_hz, chip, cs, reset, drdy);
#endif
}

}  // namespace shakepie

#endif
