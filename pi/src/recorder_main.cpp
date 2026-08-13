#include "shakepie/acquisition.hpp"
#include "shakepie/ads1256.hpp"
#include "shakepie/clocks.hpp"
#include "shakepie/config.hpp"
#include "shakepie/fake_adc.hpp"
#include "shakepie/health.hpp"
#include "shakepie/linux_hardware.hpp"
#include "shakepie/paced_source.hpp"
#include "shakepie/queues.hpp"
#include "shakepie/storage.hpp"
#include "shakepie/stream_server.hpp"

#include <atomic>
#include <charconv>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>

namespace {

volatile std::sig_atomic_t signal_received = 0;

extern "C" void handle_signal(int) {
    signal_received = 1;
}

std::size_t disk_queue_capacity(const shakepie::RecorderConfig& config) {
    const auto groups = static_cast<std::uint64_t>(config.storage.queue_seconds) *
                        config.acquisition.nominal_group_rate_hz;
    const auto blocks = (groups + config.acquisition.samples_per_block - 1U) /
                        config.acquisition.samples_per_block;
    if (blocks > std::numeric_limits<std::size_t>::max() - 2U) {
        throw std::overflow_error("configured disk queue is too large");
    }
    return static_cast<std::size_t>(blocks + 2U);
}

bool parse_offset_value(const std::string& text, std::int64_t& output) {
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), output);
    return error == std::errc{} && end == text.data() + text.size() &&
           output >= -8'388'608 && output <= 8'388'607;
}

// Reads the persisted offsets written after a successful calibration so recording
// stays centered across restarts without recalibrating.
bool read_zero_offsets(const std::filesystem::path& path, shakepie::HealthCounters& health) {
    std::ifstream input(path);
    if (!input) {
        return false;
    }
    std::int64_t north = 0;
    std::int64_t east = 0;
    std::int64_t vertical = 0;
    bool have_north = false;
    bool have_east = false;
    bool have_vertical = false;
    std::string line;
    while (std::getline(input, line)) {
        const auto comment = line.find('#');
        if (comment != std::string::npos) {
            line.erase(comment);
        }
        const auto equals = line.find('=');
        if (equals == std::string::npos) {
            continue;
        }
        const auto key = line.substr(0, equals);
        const auto value = line.substr(equals + 1);
        std::int64_t parsed = 0;
        if (!parse_offset_value(value, parsed)) {
            return false;
        }
        if (key == "north") {
            north = parsed;
            have_north = true;
        } else if (key == "east") {
            east = parsed;
            have_east = true;
        } else if (key == "vertical") {
            vertical = parsed;
            have_vertical = true;
        }
    }
    if (!have_north || !have_east || !have_vertical) {
        return false;
    }
    health.zero_offset_north.store(north, std::memory_order_relaxed);
    health.zero_offset_east.store(east, std::memory_order_relaxed);
    health.zero_offset_vertical.store(vertical, std::memory_order_relaxed);
    return true;
}

void write_zero_offsets(const std::filesystem::path& path, const shakepie::HealthCounters& health) {
    const auto temporary = path.string() + ".tmp";
    {
        std::ofstream output(temporary, std::ios::trunc);
        if (!output) {
            std::cerr << "calibration: cannot write offset state file\n";
            return;
        }
        output << "# ShakePie zero-offset calibration state. Subtracted from raw ADC counts.\n"
               << "north=" << health.zero_offset_north.load(std::memory_order_relaxed) << "\n"
               << "east=" << health.zero_offset_east.load(std::memory_order_relaxed) << "\n"
               << "vertical=" << health.zero_offset_vertical.load(std::memory_order_relaxed)
               << "\n";
    }
    std::error_code error;
    std::filesystem::rename(temporary, path, error);
    if (error) {
        std::cerr << "calibration: cannot publish offset state file: " << error.message() << '\n';
        std::filesystem::remove(temporary, error);
    }
}

// Watches the request file written by the web interface or the SSH console, forwards the
// request to the acquisition engine, and persists the offsets once calibration finishes.
// Completion is detected via the engine's calibration serial (bumped for every consumed
// request), which works identically for the instant "clear" path and the timed measurement.
// One automatic calibration also runs at every startup: after a short settling delay it
// measures over the long startup window, and those offsets are kept for the whole run.
void run_calibration_watcher(const shakepie::CalibrationConfig& config,
                             shakepie::HealthCounters& health,
                             std::atomic<bool>& calibration_requested,
                             std::atomic<bool>& calibration_clear,
                             std::atomic<bool>& calibration_startup,
                             const std::atomic<bool>& stop_requested,
                             const std::atomic<bool>& acquisition_done) {
    auto request_stuck = false;
    auto awaiting_consumption = false;
    auto awaiting_completion = false;
    auto consumed_serial = std::uint64_t{0};
    auto startup_pending = config.enabled && config.auto_on_start;
    while (!acquisition_done.load(std::memory_order_acquire)) {
        if (stop_requested.load(std::memory_order_acquire)) {
            return;
        }
        std::error_code error;
        if (startup_pending) {
            // Give the electronics a moment to settle, then start the one-shot
            // measurement over the long startup window. Restored offsets (if any) keep
            // the recorded data centered while it runs.
            auto waited = std::uint32_t{0};
            while (waited < config.startup_delay_seconds &&
                   !stop_requested.load(std::memory_order_acquire) &&
                   !acquisition_done.load(std::memory_order_acquire)) {
                std::this_thread::sleep_for(std::chrono::seconds(1));
                ++waited;
            }
            if (stop_requested.load(std::memory_order_acquire) ||
                acquisition_done.load(std::memory_order_acquire)) {
                return;
            }
            consumed_serial = health.calibration_serial.load(std::memory_order_acquire);
            calibration_startup.store(true, std::memory_order_release);
            calibration_requested.store(true, std::memory_order_release);
            awaiting_consumption = true;
            startup_pending = false;
            std::cerr << "calibration: startup calibration started (window "
                      << config.startup_window_seconds << "s)\n";
        }
        const auto request_present =
            !request_stuck && std::filesystem::exists(config.request_file, error) && !error;
        if (!awaiting_consumption && request_present) {
            std::ifstream input(config.request_file);
            std::string token;
            input >> token;
            std::filesystem::remove(config.request_file, error);
            if (error) {
                // Cannot consume the request (foreign owner, hardened path): park it
                // instead of re-triggering calibration on every poll.
                request_stuck = true;
                std::cerr << "calibration: cannot remove " << config.request_file.string()
                          << "; ignoring requests until it is removed\n";
            } else if (token == "clear") {
                // Capture the serial BEFORE raising the flag so a fast consumption can
                // never be mistaken for the baseline.
                consumed_serial = health.calibration_serial.load(std::memory_order_acquire);
                calibration_clear.store(true, std::memory_order_release);
                awaiting_consumption = true;
            } else {
                consumed_serial = health.calibration_serial.load(std::memory_order_acquire);
                calibration_requested.store(true, std::memory_order_release);
                awaiting_consumption = true;
            }
        }
        if (request_stuck && !std::filesystem::exists(config.request_file, error)) {
            request_stuck = false;
            std::cerr << "calibration: stuck request file was removed; accepting requests\n";
        }
        if (awaiting_consumption &&
            health.calibration_serial.load(std::memory_order_acquire) > consumed_serial) {
            // The engine bumps the serial when it consumes the request; a measurement's
            // outcome only arrives after the window completes, so keep waiting for a
            // terminal outcome before persisting anything.
            awaiting_consumption = false;
            awaiting_completion = true;
        }
        if (awaiting_completion) {
            const auto outcome = health.calibration_outcome.load(std::memory_order_acquire);
            if (outcome == shakepie::CalibrationOutcome::applied ||
                outcome == shakepie::CalibrationOutcome::cleared) {
                awaiting_completion = false;
                write_zero_offsets(config.state_file, health);
            } else if (outcome == shakepie::CalibrationOutcome::rejected) {
                awaiting_completion = false;
                std::cerr << "calibration: measured offset exceeds the configured limit; "
                             "offsets unchanged\n";
            }
            // CalibrationOutcome::none means the measurement window is still running.
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 3 || std::string(argv[1]) != "--config") {
        std::cerr << "usage: shakepie-recorder --config /etc/shakepie/recorder.conf\n";
        return 64;
    }
    const auto loaded = shakepie::load_config(argv[2]);
    if (!loaded.ok()) {
        for (const auto& error : loaded.errors) std::cerr << "configuration: " << error << '\n';
        return 78;
    }
    const auto mount = shakepie::verify_hdd_mount(loaded.config.storage.hdd_mount,
                                                  loaded.config.storage.data_directory);
    if (!mount.ok) {
        std::cerr << mount.error << '\n';
        return 73;
    }
    const auto recovery = shakepie::recover_partial_files(loaded.config.storage);
    for (const auto& error : recovery.errors) std::cerr << "recovery: " << error << '\n';
    if (!recovery.ok()) return 74;
    if (recovery.files_recovered != 0U || recovery.files_finalized != 0U ||
        recovery.files_quarantined != 0U) {
        std::cerr << "recovery completed: " << recovery.files_recovered << " repaired, "
                  << recovery.files_finalized << " finalized, " << recovery.files_quarantined
                  << " empty partials quarantined\n";
    }

    shakepie::SystemClock clock;
    shakepie::HealthCounters health;
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> acquisition_done{false};
    std::atomic<bool> calibration_requested{false};
    std::atomic<bool> calibration_clear{false};
    std::atomic<bool> calibration_startup{false};
    if (loaded.config.calibration.enabled && loaded.config.calibration.restore_on_start &&
        read_zero_offsets(loaded.config.calibration.state_file, health)) {
        std::cerr << "calibration: restored zero offsets from "
                  << loaded.config.calibration.state_file.string() << '\n';
    }
    std::unique_ptr<shakepie::IAds1256Transport> hardware;
    std::unique_ptr<shakepie::ISampleSource> source;
    std::string error;
    if (loaded.config.adc.backend == shakepie::AdcBackend::fake) {
        source = std::make_unique<shakepie::FakeAdc>(loaded.config.fake,
                                                     loaded.config.acquisition.nominal_group_rate_hz,
                                                     clock);
    } else {
        hardware = shakepie::make_linux_ads1256_transport(loaded.config.adc, error);
        if (!hardware) {
            std::cerr << error << '\n';
            return 69;
        }
        // The ADS1256 pipeline produces DRDY events faster than the configured group
        // rate; pacing enforces the advertised cadence so sample counts, block
        // timestamps, and the nominal rate metadata all agree with wall time.
        source = std::make_unique<shakepie::PacedSampleSource>(
            std::make_unique<shakepie::Ads1256>(loaded.config.adc, *hardware), clock,
            loaded.config.acquisition.nominal_group_rate_hz);
    }
    if (!source->initialize(error)) {
        std::cerr << "ADC initialization failed: " << error << '\n';
        return 69;
    }

    try {
        shakepie::SpscRing<shakepie::protocol::SampleBlock> disk_queue(disk_queue_capacity(loaded.config));
        shakepie::LiveOverwriteRing live_queue(loaded.config.stream.live_queue_blocks);
        shakepie::AcquisitionEngine acquisition(loaded.config.acquisition,
                                                loaded.config.calibration,
                                                *source, clock, disk_queue,
                                                live_queue, health, stop_requested,
                                                acquisition_done, calibration_requested,
                                                calibration_clear, calibration_startup);
        shakepie::StorageWriter storage(loaded.config.storage, loaded.config.adc,
                                        loaded.config.acquisition, disk_queue, health, clock,
                                        stop_requested, acquisition_done);
        shakepie::StreamServer stream(loaded.config.stream, live_queue, health,
                                      stop_requested, acquisition_done);

        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);
        health.state.store(shakepie::RecorderState::running, std::memory_order_release);

        std::thread storage_thread([&] { storage.run(); });
        std::thread stream_thread([&] { stream.run(); });
        std::thread acquisition_thread([&] { acquisition.run(); });
        std::thread calibration_thread([&] {
            run_calibration_watcher(loaded.config.calibration, health, calibration_requested,
                                    calibration_clear, calibration_startup, stop_requested,
                                    acquisition_done);
        });
        std::thread health_thread([&] {
            std::string health_error;
            while (!acquisition_done.load(std::memory_order_acquire)) {
                health.updated_realtime_ns.store(clock.realtime_ns(), std::memory_order_relaxed);
                if (!shakepie::write_health_file(loaded.config.health.status_file, health, health_error)) {
                    std::cerr << "health publication failed: " << health_error << '\n';
                    health_error.clear();
                }
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }
        });

        while (signal_received == 0 && health.state.load(std::memory_order_acquire) != shakepie::RecorderState::fatal) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        if (health.state.load(std::memory_order_relaxed) != shakepie::RecorderState::fatal) {
            health.state.store(shakepie::RecorderState::stopping, std::memory_order_release);
        }
        stop_requested.store(true, std::memory_order_release);
        acquisition_thread.join();
        calibration_thread.join();
        storage_thread.join();
        stream_thread.join();
        health_thread.join();
        if (health.state.load(std::memory_order_relaxed) != shakepie::RecorderState::fatal) {
            health.state.store(shakepie::RecorderState::stopped, std::memory_order_release);
        }
        health.updated_realtime_ns.store(clock.realtime_ns(), std::memory_order_relaxed);
        std::string health_error;
        static_cast<void>(shakepie::write_health_file(loaded.config.health.status_file, health, health_error));
        if (!storage.last_error().empty()) std::cerr << storage.last_error() << '\n';
        if (!stream.last_error().empty()) std::cerr << stream.last_error() << '\n';
        return health.state.load() == shakepie::RecorderState::fatal ? 1 : 0;
    } catch (const std::exception& exception) {
        std::cerr << "startup allocation failed: " << exception.what() << '\n';
        return 70;
    }
}
