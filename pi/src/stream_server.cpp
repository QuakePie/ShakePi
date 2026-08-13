#include "shakepie/stream_server.hpp"

#if defined(__linux__)

#include "shakepie/protocol.hpp"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <exception>
#include <iostream>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#include <fcntl.h>

namespace shakepie {
namespace {

std::string errno_message(int error_number) {
    return std::error_code(error_number == 0 ? EIO : error_number, std::generic_category()).message();
}

struct Client {
    int descriptor{-1};
    std::deque<std::vector<std::byte>> frames{};
    std::size_t sent_offset{};
    std::chrono::steady_clock::time_point last_progress{std::chrono::steady_clock::now()};
};

void close_client(Client& client) noexcept {
    if (client.descriptor >= 0) ::close(client.descriptor);
    client.descriptor = -1;
}

bool set_nonblocking(int descriptor) noexcept {
    const int flags = ::fcntl(descriptor, F_GETFL, 0);
    return flags >= 0 && ::fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0;
}

int create_listener(const StreamConfig& config, std::string& error) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_NUMERICSERV;
    addrinfo* addresses = nullptr;
    const std::string port = std::to_string(config.port);
    const int lookup = ::getaddrinfo(config.bind_address.c_str(), port.c_str(), &hints, &addresses);
    if (lookup != 0) {
        error = "stream address lookup failed: " + std::string(gai_strerror(lookup));
        return -1;
    }
    int listener = -1;
    int listener_error = EADDRNOTAVAIL;
    for (addrinfo* address = addresses; address != nullptr; address = address->ai_next) {
        listener = ::socket(address->ai_family, address->ai_socktype | SOCK_CLOEXEC, address->ai_protocol);
        if (listener < 0) {
            listener_error = errno;
            continue;
        }
        int reuse = 1;
        static_cast<void>(::setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)));
        if (::bind(listener, address->ai_addr, address->ai_addrlen) == 0 &&
            ::listen(listener, static_cast<int>(config.max_clients)) == 0 && set_nonblocking(listener)) break;
        listener_error = errno;
        ::close(listener);
        listener = -1;
    }
    ::freeaddrinfo(addresses);
    if (listener < 0) error = "cannot bind stream listener: " + errno_message(listener_error);
    return listener;
}

void configure_client(int descriptor, const StreamConfig& config) noexcept {
    int enabled = 1;
    static_cast<void>(::setsockopt(descriptor, SOL_SOCKET, SO_KEEPALIVE, &enabled, sizeof(enabled)));
    const int idle = static_cast<int>(config.keepalive_seconds);
    const int interval = std::max(1, idle / 3);
    const int probes = 3;
    static_cast<void>(::setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle)));
    static_cast<void>(::setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval)));
    static_cast<void>(::setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPCNT, &probes, sizeof(probes)));
    static_cast<void>(set_nonblocking(descriptor));
}

std::vector<std::byte> frame_block(const protocol::SampleBlock& block) {
    std::array<std::byte, protocol::kMaxEncodedBlockSize> encoded{};
    const auto size = protocol::encode_block(block, encoded);
    if (size == 0U) return {};
    std::vector<std::byte> frame(4U + size);
    const auto length = static_cast<std::uint32_t>(size);
    for (unsigned index = 0; index < 4U; ++index) {
        frame[index] = static_cast<std::byte>((length >> (index * 8U)) & 0xffU);
    }
    std::copy_n(encoded.begin(), size, frame.begin() + 4);
    return frame;
}

}  // namespace

StreamServer::StreamServer(StreamConfig config,
                           LiveOverwriteRing& queue,
                           HealthCounters& health,
                           std::atomic<bool>& stop_requested,  // NOLINT(bugprone-easily-swappable-parameters)
                           std::atomic<bool>& acquisition_done)
    : config_(std::move(config)), queue_(queue), health_(health), stop_requested_(stop_requested),
      acquisition_done_(acquisition_done) {}

void StreamServer::run() noexcept {
    int listener = -1;
    std::vector<Client> clients;
    try {
        listener = create_listener(config_, last_error_);
        if (listener < 0) {
            health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
            std::cerr << last_error_ << '\n';
            return;
        }
        clients.reserve(config_.max_clients);

        auto disconnect = [&](std::size_t index) {
            close_client(clients[index]);
            clients.erase(clients.begin() + static_cast<std::ptrdiff_t>(index));
            health_.client_disconnects.fetch_add(1U, std::memory_order_relaxed);
            health_.connected_clients.store(clients.size(), std::memory_order_relaxed);
        };

        while (!stop_requested_.load(std::memory_order_acquire) ||
               !acquisition_done_.load(std::memory_order_acquire)) {
            for (;;) {
                sockaddr_storage peer{};
                socklen_t peer_size = sizeof(peer);
                const int descriptor = ::accept4(listener, reinterpret_cast<sockaddr*>(&peer), &peer_size,
                                                 SOCK_CLOEXEC | SOCK_NONBLOCK);
                if (descriptor < 0) {
                    if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                        health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
                    }
                    break;
                }
                if (clients.size() >= config_.max_clients) {
                    ::close(descriptor);
                    continue;
                }
                configure_client(descriptor, config_);
                clients.push_back(Client{descriptor});
                health_.client_connections.fetch_add(1U, std::memory_order_relaxed);
                health_.connected_clients.store(clients.size(), std::memory_order_relaxed);
            }

            protocol::SampleBlock block{};
            while (queue_.try_pop(block)) {
                auto framed = frame_block(block);
                if (framed.empty()) {
                    health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
                    continue;
                }
                for (auto& client : clients) {
                    if (client.frames.size() >= config_.client_queue_blocks) {
                        if (client.sent_offset == 0U) {
                            client.frames.pop_front();
                        } else {
                            client.frames.erase(client.frames.begin() + 1);
                        }
                        health_.client_queue_drops.fetch_add(1U, std::memory_order_relaxed);
                    }
                    client.frames.push_back(framed);
                }
            }
            health_.live_queue_drops.fetch_add(queue_.take_consumer_drops(),
                                               std::memory_order_relaxed);

            std::vector<pollfd> polls;
            polls.reserve(clients.size() + 1U);
            polls.push_back(pollfd{listener, POLLIN, 0});
            for (const auto& client : clients) {
                short events = POLLIN;
                if (!client.frames.empty()) events = static_cast<short>(events | POLLOUT);
                polls.push_back(pollfd{client.descriptor, events, 0});
            }
            const int ready = ::poll(polls.data(), static_cast<nfds_t>(polls.size()), 20);
            if (ready < 0 && errno != EINTR) {
                health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
            }

            for (std::size_t reverse = clients.size(); reverse > 0; --reverse) {
                const std::size_t index = reverse - 1U;
                const short events = polls[index + 1U].revents;
                bool remove = (events & (POLLERR | POLLHUP | POLLNVAL)) != 0;
                if (!remove && (events & POLLIN) != 0) {
                    std::array<std::byte, 64> unexpected{};
                    const auto received =
                        ::recv(clients[index].descriptor, unexpected.data(), unexpected.size(), 0);
                    if (received >= 0 ||
                        (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                        remove = true;
                    }
                }
                if (!remove && (events & POLLOUT) != 0 && !clients[index].frames.empty()) {
                    auto& client = clients[index];
                    auto& frame = client.frames.front();
                    const auto sent = ::send(client.descriptor, frame.data() + client.sent_offset,
                                             frame.size() - client.sent_offset, MSG_NOSIGNAL);
                    if (sent > 0) {
                        client.sent_offset += static_cast<std::size_t>(sent);
                        client.last_progress = std::chrono::steady_clock::now();
                        if (client.sent_offset == frame.size()) {
                            client.frames.pop_front();
                            client.sent_offset = 0;
                        }
                    } else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK &&
                               errno != EINTR) {
                        remove = true;
                    }
                }
                if (!remove && !clients[index].frames.empty() &&
                    std::chrono::steady_clock::now() - clients[index].last_progress >
                        std::chrono::seconds(config_.write_timeout_seconds)) {
                    remove = true;
                }
                if (remove) disconnect(index);
            }
        }
    } catch (const std::exception& error) {
        try {
            last_error_ = "stream worker exception: ";
            last_error_ += error.what();
        } catch (...) {
        }
        std::cerr << "stream worker exception: " << error.what() << '\n';
        health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
    } catch (...) {
        try {
            last_error_ = "stream worker stopped after an unknown exception";
        } catch (...) {
        }
        std::cerr << "stream worker stopped after an unknown exception\n";
        health_.stream_errors.fetch_add(1U, std::memory_order_relaxed);
    }

    for (auto& client : clients) close_client(client);
    health_.connected_clients.store(0U, std::memory_order_relaxed);
    if (listener >= 0) ::close(listener);
}

}  // namespace shakepie

#endif
