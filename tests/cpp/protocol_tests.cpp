#include "shakepie/protocol.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <string>
#include <vector>

namespace {

int failures = 0;

void check(bool condition, const char* expression, int line) {
    if (!condition) {
        std::cerr << "line " << line << ": check failed: " << expression << '\n';
        ++failures;
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

std::vector<std::byte> read_fixture() {
    const std::filesystem::path path =
        std::filesystem::path(SHAKEPIE_SOURCE_DIR) / "protocol" / "fixtures" / "golden-block-v1.spb";
    std::ifstream input(path, std::ios::binary);
    const std::vector<char> chars((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    std::vector<std::byte> result(chars.size());
    for (std::size_t index = 0; index < chars.size(); ++index) {
        result[index] = static_cast<std::byte>(static_cast<unsigned char>(chars[index]));
    }
    return result;
}

}  // namespace

int main() {
    using namespace shakepie::protocol;
    const auto fixture = read_fixture();
    CHECK(fixture.size() == encoded_block_size(3));

    SampleBlock decoded{};
    CHECK(decode_block(fixture, decoded) == DecodeError::none);
    CHECK(decoded.header.status_flags == 5U);
    CHECK(decoded.header.block_sequence == 42U);
    CHECK(decoded.header.first_sample_sequence == 1000U);
    CHECK(decoded.header.utc_start_nanoseconds == 1700000000123456789ULL);
    CHECK(decoded.header.monotonic_start_nanoseconds == 9876543210ULL);
    CHECK(decoded.header.nominal_group_rate_millihz == 750000U);
    CHECK(decoded.samples[0] == (SampleGroup{-8388608, 0, 8388607, 0}));
    CHECK(decoded.samples[1] == (SampleGroup{-123456, 654321, -1, 1333333}));
    CHECK(decoded.samples[2] == (SampleGroup{42, -42, 2000000, 2666666}));

    std::array<std::byte, kMaxEncodedBlockSize> encoded{};
    const std::size_t size = encode_block(decoded, encoded);
    CHECK(size == fixture.size());
    CHECK(std::equal(encoded.begin(), encoded.begin() + static_cast<std::ptrdiff_t>(size), fixture.begin()));

    auto corrupt = fixture;
    corrupt.back() ^= std::byte{0x80};
    CHECK(decode_block(corrupt, decoded) == DecodeError::invalid_crc);

    SampleBlock invalid = decoded;
    invalid.header.sample_count = 0;
    CHECK(encode_block(invalid, encoded) == 0U);
    invalid = decoded;
    invalid.header.first_sample_sequence = std::numeric_limits<std::uint64_t>::max();
    CHECK(encode_block(invalid, encoded) == 0U);
    invalid = decoded;
    invalid.samples[2].nanoseconds_from_block_start = 1U;
    CHECK(encode_block(invalid, encoded) == 0U);
    invalid = decoded;
    invalid.samples[1].north_count = std::numeric_limits<std::int32_t>::max();
    CHECK(encode_block(invalid, encoded) == 0U);

    return failures == 0 ? 0 : 1;
}
