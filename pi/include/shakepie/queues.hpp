#pragma once

#include "shakepie/protocol.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>

namespace shakepie {

template <typename T>
// The cache-line separation of producer and consumer atomics is intentional.
class SpscRing {  // NOLINT(clang-analyzer-optin.performance.Padding)
public:
    explicit SpscRing(std::size_t capacity)
        : capacity_(capacity), slots_(std::make_unique<T[]>(capacity)) {
        if (capacity < 2U) throw std::invalid_argument("SPSC capacity must be at least two");
    }

    SpscRing(const SpscRing&) = delete;
    SpscRing& operator=(const SpscRing&) = delete;

    [[nodiscard]] bool try_push(const T& value) noexcept {
        const auto head = head_.load(std::memory_order_relaxed);
        const auto tail = tail_.load(std::memory_order_acquire);
        if (head - tail >= capacity_) return false;
        slots_[static_cast<std::size_t>(head % capacity_)] = value;
        head_.store(head + 1U, std::memory_order_release);
        return true;
    }

    [[nodiscard]] bool try_pop(T& value) noexcept {
        const auto tail = tail_.load(std::memory_order_relaxed);
        const auto head = head_.load(std::memory_order_acquire);
        if (tail == head) return false;
        value = slots_[static_cast<std::size_t>(tail % capacity_)];
        tail_.store(tail + 1U, std::memory_order_release);
        return true;
    }

    [[nodiscard]] std::size_t size() const noexcept {
        const auto head = head_.load(std::memory_order_acquire);
        const auto tail = tail_.load(std::memory_order_acquire);
        return static_cast<std::size_t>(head - tail);
    }

    [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

private:
    const std::size_t capacity_;
    std::unique_ptr<T[]> slots_;
    alignas(64) std::atomic<std::uint64_t> head_{0};
    alignas(64) std::atomic<std::uint64_t> tail_{0};
};

// One producer and one consumer. The producer never waits. It first uses an
// empty slot; only when none is available does it atomically reclaim the
// oldest ready slot. A slot being copied by the consumer is never overwritten.
class LiveOverwriteRing {
public:
    struct PushResult {
        bool stored{};
        bool dropped_oldest{};
    };

    explicit LiveOverwriteRing(std::size_t capacity)
        : capacity_(capacity), slots_(std::make_unique<Slot[]>(capacity)) {
        if (capacity < 2U) throw std::invalid_argument("live capacity must be at least two");
    }

    LiveOverwriteRing(const LiveOverwriteRing&) = delete;
    LiveOverwriteRing& operator=(const LiveOverwriteRing&) = delete;

    [[nodiscard]] PushResult push(const protocol::SampleBlock& block) noexcept {
        for (std::size_t step = 0; step < capacity_; ++step) {
            Slot& slot = slots_[(cursor_ + step) % capacity_];
            State expected = State::empty;
            if (slot.state.compare_exchange_strong(expected, State::writing, std::memory_order_acquire)) {
                publish(slot, block);
                cursor_ = (cursor_ + step + 1U) % capacity_;
                return {true, false};
            }
        }

        for (std::size_t retry = 0; retry < capacity_; ++retry) {
            for (std::size_t step = 0; step < capacity_; ++step) {
                Slot& empty_slot = slots_[(cursor_ + step) % capacity_];
                State empty = State::empty;
                if (empty_slot.state.compare_exchange_strong(
                        empty, State::writing, std::memory_order_acquire)) {
                    publish(empty_slot, block);
                    cursor_ = (cursor_ + step + 1U) % capacity_;
                    return {true, false};
                }
            }
            std::uint64_t oldest = std::numeric_limits<std::uint64_t>::max();
            std::size_t oldest_index = capacity_;
            for (std::size_t index = 0; index < capacity_; ++index) {
                if (slots_[index].state.load(std::memory_order_acquire) == State::ready) {
                    const auto sequence = slots_[index].sequence.load(std::memory_order_acquire);
                    if (sequence < oldest) {
                        oldest = sequence;
                        oldest_index = index;
                    }
                }
            }
            if (oldest_index == capacity_) return {false, true};

            State expected = State::ready;
            Slot& slot = slots_[oldest_index];
            if (!slot.state.compare_exchange_strong(
                    expected, State::writing, std::memory_order_acquire)) {
                continue;
            }
            publish(slot, block);
            cursor_ = (oldest_index + 1U) % capacity_;
            return {true, true};
        }
        return {false, true};
    }

    [[nodiscard]] bool try_pop(protocol::SampleBlock& block) noexcept {
        for (std::size_t retry = 0; retry < capacity_ * 2U; ++retry) {
            std::uint64_t oldest = std::numeric_limits<std::uint64_t>::max();
            std::size_t oldest_index = capacity_;
            for (std::size_t index = 0; index < capacity_; ++index) {
                if (slots_[index].state.load(std::memory_order_acquire) == State::ready) {
                    const auto sequence = slots_[index].sequence.load(std::memory_order_acquire);
                    if (sequence < oldest) {
                        oldest = sequence;
                        oldest_index = index;
                    }
                }
            }
            if (oldest_index == capacity_) return false;
            Slot& slot = slots_[oldest_index];
            State expected = State::ready;
            if (!slot.state.compare_exchange_strong(expected, State::reading, std::memory_order_acquire)) {
                continue;
            }
            if (slot.sequence.load(std::memory_order_acquire) != oldest) {
                slot.state.store(State::ready, std::memory_order_release);
                continue;
            }
            block = slot.block;
            if (block.header.block_sequence != oldest ||
                (have_last_popped_ && oldest <= last_popped_sequence_)) {
                ++consumer_drops_;
                slot.state.store(State::empty, std::memory_order_release);
                continue;
            }
            last_popped_sequence_ = oldest;
            have_last_popped_ = true;
            slot.state.store(State::empty, std::memory_order_release);
            return true;
        }
        return false;
    }

    [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

    [[nodiscard]] std::uint64_t take_consumer_drops() noexcept {
        const auto result = consumer_drops_;
        consumer_drops_ = 0;
        return result;
    }

private:
    enum class State : std::uint8_t { empty, writing, ready, reading };
    struct Slot {
        std::atomic<State> state{State::empty};
        std::atomic<std::uint64_t> sequence{0};
        protocol::SampleBlock block{};
    };

    static void publish(Slot& slot, const protocol::SampleBlock& block) noexcept {
        slot.block = block;
        slot.sequence.store(block.header.block_sequence, std::memory_order_release);
        slot.state.store(State::ready, std::memory_order_release);
    }

    const std::size_t capacity_;
    std::unique_ptr<Slot[]> slots_;
    std::size_t cursor_{0};
    std::uint64_t last_popped_sequence_{0};
    std::uint64_t consumer_drops_{0};
    bool have_last_popped_{false};
};

}  // namespace shakepie
