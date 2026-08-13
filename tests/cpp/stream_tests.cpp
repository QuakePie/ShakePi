#include "test_harness.hpp"

#include "shakepie/config.hpp"
#include "shakepie/health.hpp"
#include "shakepie/protocol.hpp"
#include "shakepie/queues.hpp"
#include "shakepie/stream_server.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <thread>

namespace {

using namespace std::chrono_literals;

std::uint16_t reserve_loopback_port() {
    const int descriptor = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (descriptor < 0) return 0;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (::bind(descriptor, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        ::close(descriptor);
        return 0;
    }
    socklen_t size = sizeof(address);
    if (::getsockname(descriptor, reinterpret_cast<sockaddr*>(&address), &size) != 0) {
        ::close(descriptor);
        return 0;
    }
    ::close(descriptor);
    return ntohs(address.sin_port);
}

int occupy_loopback_port(std::uint16_t& port) {
    const int descriptor = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (descriptor < 0) return -1;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    socklen_t size = sizeof(address);
    if (::bind(descriptor, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0 ||
        ::getsockname(descriptor, reinterpret_cast<sockaddr*>(&address), &size) != 0 ||
        ::listen(descriptor, 1) != 0) {
        ::close(descriptor);
        return -1;
    }
    port = ntohs(address.sin_port);
    return descriptor;
}

int connect_with_retry(std::uint16_t port) {
    for (unsigned attempt = 0; attempt < 100U; ++attempt) {
        const int descriptor = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (descriptor >= 0) {
            sockaddr_in address{};
            address.sin_family = AF_INET;
            address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            address.sin_port = htons(port);
            if (::connect(descriptor, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0) {
                return descriptor;
            }
            ::close(descriptor);
        }
        std::this_thread::sleep_for(10ms);
    }
    return -1;
}

bool wait_for(const std::atomic<std::uint64_t>& value, std::uint64_t expected) {
    for (unsigned attempt = 0; attempt < 100U; ++attempt) {
        if (value.load(std::memory_order_relaxed) == expected) return true;
        std::this_thread::sleep_for(10ms);
    }
    return false;
}

shakepie::protocol::SampleBlock make_block(std::uint64_t sequence) {
    shakepie::protocol::SampleBlock block{};
    block.header.block_sequence = sequence;
    block.header.first_sample_sequence = sequence * shakepie::protocol::kMaxSamplesPerBlock;
    block.header.utc_start_nanoseconds = sequence;
    block.header.monotonic_start_nanoseconds = sequence;
    block.header.sample_count = shakepie::protocol::kMaxSamplesPerBlock;
    block.header.nominal_group_rate_millihz = 750'000;
    return block;
}

}  // namespace

int main() {
    shakepie::StreamConfig config{};
    config.port = reserve_loopback_port();
    config.live_queue_blocks = 1'024;
    config.client_queue_blocks = 2;
    config.max_clients = 2;
    config.write_timeout_seconds = 1;
    CHECK(config.port != 0U);

    shakepie::LiveOverwriteRing queue(config.live_queue_blocks);
    shakepie::HealthCounters health;
    std::atomic<bool> stop{false};
    std::atomic<bool> done{false};
    shakepie::StreamServer server(config, queue, health, stop, done);
    std::thread server_thread([&server] { server.run(); });

    const int invalid_client = connect_with_retry(config.port);
    CHECK(invalid_client >= 0);
    CHECK(wait_for(health.connected_clients, 1U));
    const char unexpected = 'x';
    CHECK(::send(invalid_client, &unexpected, 1, 0) == 1);
    CHECK(wait_for(health.connected_clients, 0U));
    if (invalid_client >= 0) ::close(invalid_client);

    const int slow_client = connect_with_retry(config.port);
    CHECK(slow_client >= 0);
    const int receive_buffer = 1'024;
    CHECK(::setsockopt(slow_client, SOL_SOCKET, SO_RCVBUF, &receive_buffer,
                       sizeof(receive_buffer)) == 0);
    CHECK(wait_for(health.connected_clients, 1U));
    std::uint64_t producer_drops = 0;
    for (std::uint64_t sequence = 0; sequence < 5'000U; ++sequence) {
        const auto result = queue.push(make_block(sequence));
        if (result.dropped_oldest) ++producer_drops;
    }
    for (unsigned attempt = 0; attempt < 100U && health.client_queue_drops.load() == 0U;
         ++attempt) {
        std::this_thread::sleep_for(10ms);
    }
    CHECK(producer_drops != 0U || health.client_queue_drops.load() != 0U);
    CHECK(health.client_queue_drops.load() != 0U);
    CHECK(health.stream_errors.load() == 0U);

    if (slow_client >= 0) ::close(slow_client);
    stop.store(true, std::memory_order_release);
    done.store(true, std::memory_order_release);
    server_thread.join();
    CHECK(health.connected_clients.load() == 0U);
    CHECK(server.last_error().empty());

    std::uint16_t occupied_port = 0;
    const int occupied = occupy_loopback_port(occupied_port);
    CHECK(occupied >= 0 && occupied_port != 0U);
    shakepie::StreamConfig conflict_config{};
    conflict_config.port = occupied_port;
    shakepie::LiveOverwriteRing conflict_queue(2);
    shakepie::HealthCounters conflict_health;
    std::atomic<bool> conflict_stop{true};
    std::atomic<bool> conflict_done{true};
    shakepie::StreamServer conflict_server(conflict_config, conflict_queue, conflict_health,
                                           conflict_stop, conflict_done);
    conflict_server.run();
    CHECK(conflict_health.stream_errors.load() == 1U);
    CHECK(conflict_health.fatal_code.load() == shakepie::FatalCode::none);
    CHECK(!conflict_server.last_error().empty());
    if (occupied >= 0) ::close(occupied);
    return shakepie_test_failures == 0 ? 0 : 1;
}
