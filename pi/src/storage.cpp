#include "shakepie/storage.hpp"

#include "shakepie/protocol.hpp"

#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <system_error>
#include <thread>
#include <utility>

#include <fcntl.h>

namespace shakepie {
namespace {

constexpr std::uint32_t kRecoveryFooterFlag = 1U;

std::string errno_message(int error_number) {
    return std::error_code(error_number == 0 ? EIO : error_number, std::generic_category()).message();
}

class ScopedDescriptor {
public:
    explicit ScopedDescriptor(int descriptor) noexcept : descriptor_(descriptor) {}
    ~ScopedDescriptor() { if (descriptor_ >= 0) ::close(descriptor_); }
    ScopedDescriptor(const ScopedDescriptor&) = delete;
    ScopedDescriptor& operator=(const ScopedDescriptor&) = delete;
    [[nodiscard]] int get() const noexcept { return descriptor_; }
    [[nodiscard]] bool close() noexcept {
        if (descriptor_ < 0) return true;
        const int descriptor = descriptor_;
        descriptor_ = -1;
        return ::close(descriptor) == 0;
    }

private:
    int descriptor_;
};

void put_u32(std::byte* output, std::uint32_t value) noexcept {
    for (unsigned index = 0; index < 4U; ++index) {
        output[index] = static_cast<std::byte>((value >> (index * 8U)) & 0xffU);
    }
}

std::uint32_t get_u32(const std::byte* input) noexcept {
    std::uint32_t value = 0;
    for (unsigned index = 0; index < 4U; ++index) {
        value |= static_cast<std::uint32_t>(std::to_integer<unsigned>(input[index])) << (index * 8U);
    }
    return value;
}

bool pread_exact(int descriptor, std::span<std::byte> output, std::uint64_t offset, int& error_number) noexcept {
    std::size_t completed = 0;
    while (completed < output.size()) {
        const auto result = ::pread(descriptor, output.data() + completed, output.size() - completed,
                                    static_cast<off_t>(offset + completed));
        if (result > 0) {
            completed += static_cast<std::size_t>(result);
            continue;
        }
        if (result < 0 && errno == EINTR) continue;
        error_number = result == 0 ? ENODATA : errno;
        return false;
    }
    return true;
}

bool descriptor_write_all(int descriptor, std::span<const std::byte> data, int& error_number) noexcept {
    return write_all(data, [descriptor](const std::byte* bytes, std::size_t size) {
        return ::write(descriptor, bytes, size);
    }, error_number);
}

std::string unescape_mount_path(std::string path) {
    const std::array<std::pair<std::string_view, char>, 4> replacements{{
        {"\\040", ' '}, {"\\011", '\t'}, {"\\012", '\n'}, {"\\134", '\\'},
    }};
    for (const auto& [escaped, value] : replacements) {
        std::size_t position = 0;
        while ((position = path.find(escaped, position)) != std::string::npos) {
            path.replace(position, escaped.size(), 1U, value);
            ++position;
        }
    }
    return path;
}

std::filesystem::path final_name_for(const std::filesystem::path& partial, bool recovered) {
    auto name = partial.filename().string();
    const std::string suffix = ".partial";
    if (name.ends_with(suffix)) name.erase(name.size() - suffix.size());
    name += recovered ? ".recovered.spb" : ".spb";
    return partial.parent_path() / name;
}

std::string utc_file_stem(std::uint64_t utc_ns) {
    const auto seconds = static_cast<std::time_t>(utc_ns / 1'000'000'000ULL);
    std::tm value{};
    gmtime_r(&seconds, &value);
    std::ostringstream output;
    output << std::put_time(&value, "%Y%m%dT%H%M%SZ");
    return output.str();
}

struct ScanResult {
    file_format::FileHeader header{};
    file_format::FileFooter footer{};
    std::uint64_t last_good_offset{file_format::kFileHeaderSize};
    bool empty_file{};
    bool valid_header{};
    bool has_blocks{};
    bool valid_footer{};
    std::string error{};
};

ScanResult scan_recording(int descriptor, std::uint64_t file_size) {
    ScanResult scan{};
    if (file_size == 0U) {
        scan.empty_file = true;
        return scan;
    }
    std::array<std::byte, file_format::kFileHeaderSize> header_bytes{};
    int io_error = 0;
    if (file_size < header_bytes.size() || !pread_exact(descriptor, header_bytes, 0, io_error)) {
        scan.error = "truncated file header";
        return scan;
    }
    const auto header_error = file_format::decode_header(header_bytes, scan.header);
    if (header_error != file_format::FileDecodeError::none) {
        scan.error = std::string(file_format::decode_error_message(header_error));
        return scan;
    }
    scan.valid_header = true;

    std::uint64_t offset = file_format::kFileHeaderSize;
    bool have_block = false;
    std::uint64_t previous_block = 0;
    std::uint64_t previous_last_sample = 0;
    while (offset + 4U <= file_size) {
        std::array<std::byte, 4> prefix{};
        if (!pread_exact(descriptor, prefix, offset, io_error)) break;
        if (prefix == std::array<std::byte, 4>{std::byte{'S'}, std::byte{'P'}, std::byte{'F'}, std::byte{'T'}}) {
            if (offset + file_format::kFileFooterSize > file_size) break;
            std::array<std::byte, file_format::kFileFooterSize> footer_bytes{};
            if (!pread_exact(descriptor, footer_bytes, offset, io_error)) break;
            file_format::FileFooter disk_footer{};
            if (file_format::decode_footer(footer_bytes, disk_footer) == file_format::FileDecodeError::none &&
                have_block && offset + file_format::kFileFooterSize == file_size &&
                disk_footer.block_count == scan.footer.block_count &&
                disk_footer.sample_count == scan.footer.sample_count &&
                disk_footer.first_block_sequence == scan.header.first_block_sequence &&
                disk_footer.first_sample_sequence == scan.header.first_sample_sequence &&
                disk_footer.last_block_sequence == previous_block &&
                disk_footer.last_sample_sequence == previous_last_sample) {
                scan.footer = disk_footer;
                scan.valid_footer = true;
                scan.last_good_offset = offset + file_format::kFileFooterSize;
            }
            return scan;
        }
        const std::uint32_t length = get_u32(prefix.data());
        if (length < protocol::kBlockHeaderSize + protocol::kBlockCrcSize ||
            length > protocol::kMaxEncodedBlockSize || offset + 4U + length > file_size) break;
        std::vector<std::byte> encoded(length);
        if (!pread_exact(descriptor, encoded, offset + 4U, io_error)) break;
        protocol::SampleBlock block{};
        if (protocol::decode_block(encoded, block) != protocol::DecodeError::none) break;
        const auto last_sample = block.header.first_sample_sequence + block.header.sample_count - 1U;
        if (have_block && (block.header.block_sequence != previous_block + 1U ||
                           block.header.first_sample_sequence <= previous_last_sample)) break;
        if (!have_block) {
            if (block.header.block_sequence != scan.header.first_block_sequence ||
                block.header.first_sample_sequence != scan.header.first_sample_sequence) break;
            scan.footer.first_block_sequence = block.header.block_sequence;
            scan.footer.first_sample_sequence = block.header.first_sample_sequence;
        }
        have_block = true;
        scan.has_blocks = true;
        previous_block = block.header.block_sequence;
        previous_last_sample = last_sample;
        ++scan.footer.block_count;
        scan.footer.sample_count += block.header.sample_count;
        scan.footer.last_block_sequence = previous_block;
        scan.footer.last_sample_sequence = previous_last_sample;
        offset += 4U + length;
        scan.last_good_offset = offset;
    }
    return scan;
}

}  // namespace

bool write_all(std::span<const std::byte> data,
               const WriteOperation& operation,
               int& error_number) noexcept {
    std::size_t completed = 0;
    while (completed < data.size()) {
        const ssize_t result = operation(data.data() + completed, data.size() - completed);
        if (result > 0) {
            completed += static_cast<std::size_t>(result);
            continue;
        }
        if (result < 0 && errno == EINTR) continue;
        error_number = result == 0 ? EIO : errno;
        return false;
    }
    error_number = 0;
    return true;
}

MountCheck verify_hdd_mount(const std::filesystem::path& mount,  // NOLINT(bugprone-easily-swappable-parameters)
                            const std::filesystem::path& data_directory) {
    std::error_code error;
    if (!std::filesystem::is_directory(mount, error) || error) {
        return {false, "configured HDD mount is absent or not a directory: " + mount.string()};
    }
    const auto canonical_mount = std::filesystem::canonical(mount, error);
    if (error) return {false, "cannot resolve HDD mount: " + error.message()};
    if (!std::filesystem::is_directory(data_directory, error) || error) {
        return {false, "configured HDD data directory is absent or not a directory: " +
                           data_directory.string()};
    }
    const auto canonical_data = std::filesystem::canonical(data_directory, error);
    if (error) return {false, "cannot resolve HDD data directory: " + error.message()};
    const auto relative = canonical_data.lexically_relative(canonical_mount);
    if (canonical_data == canonical_mount || relative.empty() || *relative.begin() == "..") {
        return {false, "data directory is outside configured HDD mount"};
    }

    std::ifstream mountinfo("/proc/self/mountinfo");
    if (!mountinfo) return {false, "cannot read /proc/self/mountinfo"};
    std::string line;
    bool found = false;
    while (std::getline(mountinfo, line)) {
        std::istringstream fields(line);
        std::string value;
        for (int index = 0; index <= 4 && fields >> value; ++index) {
            if (index == 4) {
                const auto candidate = std::filesystem::path(unescape_mount_path(value));
                const auto canonical_candidate = std::filesystem::weakly_canonical(candidate, error);
                if (!error && canonical_candidate == canonical_mount) found = true;
                error.clear();
            }
        }
        if (found) break;
    }
    if (!found) return {false, "configured HDD path is not an active mount point: " + mount.string()};

    struct statvfs statistics {};
    if (::statvfs(canonical_mount.c_str(), &statistics) != 0) {
        return {false, "cannot query HDD filesystem: " + errno_message(errno)};
    }
    if ((statistics.f_flag & ST_RDONLY) != 0U) return {false, "configured HDD is read-only"};
    return {true, {}};
}

RecoveryReport recover_partial_files(const StorageConfig& config) {
    RecoveryReport report{};
    const auto mount = verify_hdd_mount(config.hdd_mount, config.data_directory);
    if (!mount.ok) {
        report.errors.push_back(mount.error);
        return report;
    }
    const int directory_descriptor =
        ::open(config.data_directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory_descriptor < 0) {
        report.errors.push_back("cannot open HDD data directory for recovery: " +
                                errno_message(errno));
        return report;
    }
    ScopedDescriptor directory_guard(directory_descriptor);
    const auto recheck = verify_hdd_mount(config.hdd_mount, config.data_directory);
    struct stat directory_information {};
    struct stat mount_information {};
    if (!recheck.ok || ::fstat(directory_descriptor, &directory_information) != 0 ||
        ::stat(config.hdd_mount.c_str(), &mount_information) != 0 ||
        directory_information.st_dev != mount_information.st_dev) {
        report.errors.push_back(recheck.ok
                                    ? "HDD data directory changed filesystems during recovery"
                                    : recheck.error);
        if (!directory_guard.close()) {
            report.errors.push_back("cannot close HDD data directory after failed recovery check");
        }
        return report;
    }
    const auto guarded_directory =
        std::filesystem::path("/proc/self/fd") / std::to_string(directory_descriptor);
    std::error_code filesystem_error;
    std::filesystem::directory_iterator entries(guarded_directory, filesystem_error);
    if (filesystem_error) {
        report.errors.push_back("cannot enumerate data directory: " + filesystem_error.message());
        if (!directory_guard.close()) {
            report.errors.push_back("cannot close HDD data directory after enumeration failure");
        }
        return report;
    }
    for (auto iterator = entries; iterator != std::filesystem::directory_iterator{};
         iterator.increment(filesystem_error)) {
        if (filesystem_error) {
            break;
        }
        const auto& entry = *iterator;
        const auto filename = entry.path().filename().string();
        const auto display_path = config.data_directory / filename;
        const bool regular = entry.is_regular_file(filesystem_error);
        if (filesystem_error) {
            report.errors.push_back(display_path.string() + ": cannot inspect partial candidate: " +
                                    filesystem_error.message());
            filesystem_error.clear();
            continue;
        }
        if (!regular || !filename.ends_with(".partial")) continue;
        ++report.files_seen;
        const int descriptor = ::openat(directory_descriptor, filename.c_str(),
                                        O_RDWR | O_CLOEXEC | O_NOFOLLOW);
        if (descriptor < 0) {
            report.errors.push_back(display_path.string() + ": open failed: " +
                                    errno_message(errno));
            continue;
        }
        struct stat information {};
        if (::fstat(descriptor, &information) != 0 || information.st_size < 0) {
            report.errors.push_back(display_path.string() + ": stat failed");
            ::close(descriptor);
            continue;
        }
        const auto original_size = static_cast<std::uint64_t>(information.st_size);
        auto scan = scan_recording(descriptor, original_size);
        if (!scan.error.empty()) {
            report.errors.push_back(display_path.string() + ": " + scan.error);
            ::close(descriptor);
            continue;
        }
        if (scan.empty_file || (scan.valid_header && !scan.has_blocks)) {
            if (::close(descriptor) != 0) {
                report.errors.push_back(display_path.string() +
                                        ": close failed while quarantining");
                continue;
            }
            const std::string destination_name = filename + ".empty";
            struct stat destination_information {};
            if (::fstatat(directory_descriptor, destination_name.c_str(),
                          &destination_information, AT_SYMLINK_NOFOLLOW) == 0 ||
                errno != ENOENT) {
                report.errors.push_back(display_path.string() +
                                        ": empty-partial destination already exists or is inaccessible");
                continue;
            }
            if (::renameat(directory_descriptor, filename.c_str(), directory_descriptor,
                           destination_name.c_str()) != 0 ||
                ::fsync(directory_descriptor) != 0) {
                report.errors.push_back(display_path.string() +
                                        ": cannot quarantine empty partial: " + errno_message(errno));
                continue;
            }
            ++report.files_quarantined;
            continue;
        }
        bool success = true;
        const bool needs_repair = !scan.valid_footer;
        const bool recovered = needs_repair ||
                               (scan.footer.footer_flags & kRecoveryFooterFlag) != 0U;
        if (needs_repair) {
            const auto truncated_bytes = original_size - scan.last_good_offset;
            if (::ftruncate(descriptor, static_cast<off_t>(scan.last_good_offset)) != 0 ||
                ::lseek(descriptor, 0, SEEK_END) < 0) {
                success = false;
            } else {
                report.bytes_truncated += truncated_bytes;
                scan.footer.footer_flags |= kRecoveryFooterFlag;
                std::array<std::byte, file_format::kFileFooterSize> footer_bytes{};
                int io_error = 0;
                success = file_format::encode_footer(scan.footer, footer_bytes) &&
                          descriptor_write_all(descriptor, footer_bytes, io_error) &&
                          ::fdatasync(descriptor) == 0;
            }
        }
        if (::close(descriptor) != 0) success = false;
        if (!success) {
            report.errors.push_back(display_path.string() + ": recovery write or sync failed");
            continue;
        }
        const auto destination_name = final_name_for(std::filesystem::path(filename), recovered)
                                          .filename()
                                          .string();
        const auto destination = config.data_directory / destination_name;
        struct stat destination_information {};
        if (::fstatat(directory_descriptor, destination_name.c_str(),
                      &destination_information, AT_SYMLINK_NOFOLLOW) == 0 ||
            errno != ENOENT) {
            report.errors.push_back(destination.string() +
                                    ": destination already exists or is inaccessible; partial preserved");
            continue;
        }
        if (::renameat(directory_descriptor, filename.c_str(), directory_descriptor,
                       destination_name.c_str()) != 0) {
            report.errors.push_back(display_path.string() + ": recovery rename failed: " +
                                    errno_message(errno));
            continue;
        }
        if (::fsync(directory_descriptor) != 0) {
            report.errors.push_back(destination.string() + ": directory sync failed: " +
                                    errno_message(errno));
            continue;
        }
        if (recovered) ++report.files_recovered;
        else ++report.files_finalized;
    }
    if (filesystem_error) {
        report.errors.push_back("cannot enumerate data directory: " + filesystem_error.message());
    }
    if (!directory_guard.close()) {
        report.errors.push_back("cannot close HDD data directory after recovery");
    }
    return report;
}

StorageWriter::StorageWriter(StorageConfig storage_config,
                             AdcConfig adc_config,
                             AcquisitionConfig acquisition_config,
                             SpscRing<protocol::SampleBlock>& queue,
                             HealthCounters& health,
                             IClock& clock,
                             std::atomic<bool>& stop_requested,  // NOLINT(bugprone-easily-swappable-parameters)
                             std::atomic<bool>& acquisition_done)
    : storage_config_(std::move(storage_config)), adc_config_(std::move(adc_config)),
      acquisition_config_(acquisition_config), queue_(queue), health_(health), clock_(clock),
      stop_requested_(stop_requested), acquisition_done_(acquisition_done) {
    aggregate_.reserve(storage_config_.aggregate_bytes + protocol::kMaxEncodedBlockSize + 4U);
}

StorageWriter::~StorageWriter() {
    if (descriptor_ >= 0) ::close(descriptor_);
    if (directory_descriptor_ >= 0) ::close(directory_descriptor_);
}

void StorageWriter::fail(FatalCode code, std::string message) noexcept {
    failed_ = true;
    last_error_ = std::move(message);
    health_.storage_errors.fetch_add(1U, std::memory_order_relaxed);
    health_.set_fatal(code);
    stop_requested_.store(true, std::memory_order_release);
}

bool StorageWriter::open_file(const protocol::SampleBlock& first) noexcept {
    const auto mount = verify_hdd_mount(storage_config_.hdd_mount, storage_config_.data_directory);
    if (!mount.ok) {
        fail(FatalCode::hdd_unavailable, mount.error);
        return false;
    }
    directory_descriptor_ =
        ::open(storage_config_.data_directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory_descriptor_ < 0) {
        fail(FatalCode::hdd_unavailable,
             "cannot open configured HDD data directory: " + errno_message(errno));
        return false;
    }
    const auto recheck = verify_hdd_mount(storage_config_.hdd_mount,
                                          storage_config_.data_directory);
    struct stat directory_information {};
    struct stat mount_information {};
    if (!recheck.ok || ::fstat(directory_descriptor_, &directory_information) != 0 ||
        ::stat(storage_config_.hdd_mount.c_str(), &mount_information) != 0 ||
        directory_information.st_dev != mount_information.st_dev) {
        const std::string reason = recheck.ok
                                       ? "HDD data directory changed filesystems while opening"
                                       : recheck.error;
        ::close(directory_descriptor_);
        directory_descriptor_ = -1;
        fail(FatalCode::hdd_unavailable, reason);
        return false;
    }
    const auto rotation_ns = static_cast<std::uint64_t>(storage_config_.rotation_minutes) * 60ULL * 1'000'000'000ULL;
    rotation_bucket_ = first.header.utc_start_nanoseconds / rotation_ns;
    const auto bucket_start = rotation_bucket_ * rotation_ns;
    const std::string stem = storage_config_.station_id + "_" + utc_file_stem(bucket_start);
    auto open_candidate = [&](const std::string& candidate) {
        partial_path_ = storage_config_.data_directory / (candidate + ".partial");
        final_path_ = storage_config_.data_directory / (candidate + ".spb");
        struct stat information {};
        const auto final_leaf = final_path_.filename().string();
        if (::fstatat(directory_descriptor_, final_leaf.c_str(), &information,
                      AT_SYMLINK_NOFOLLOW) == 0) {
            errno = EEXIST;
            return -1;
        }
        if (errno != ENOENT) return -1;
        const auto partial_leaf = partial_path_.filename().string();
        return ::openat(directory_descriptor_, partial_leaf.c_str(),
                        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0640);
    };
    descriptor_ = open_candidate(stem);
    if (descriptor_ < 0 && errno == EEXIST) {
        const std::string unique = stem + "_u" + std::to_string(first.header.utc_start_nanoseconds);
        descriptor_ = open_candidate(unique);
    }
    if (descriptor_ < 0) {
        const int failure = errno;
        ::close(directory_descriptor_);
        directory_descriptor_ = -1;
        fail(FatalCode::storage_open, "cannot open partial recording: " + errno_message(failure));
        return false;
    }
    file_format::FileHeader header{};
    header.pga_gain = static_cast<std::uint16_t>(adc_config_.pga_gain);
    header.adc_data_rate_millihz = static_cast<std::uint32_t>(adc_config_.data_rate_sps * 1000.0);
    header.reference_voltage_uv = adc_config_.reference_voltage_uv;
    header.samples_per_block = acquisition_config_.samples_per_block;
    header.created_utc_ns = first.header.utc_start_nanoseconds;
    header.created_monotonic_ns = first.header.monotonic_start_nanoseconds;
    header.first_block_sequence = first.header.block_sequence;
    header.first_sample_sequence = first.header.first_sample_sequence;
    header.station_id = storage_config_.station_id;
    std::array<std::byte, file_format::kFileHeaderSize> encoded{};
    int io_error = 0;
    const bool encoded_header = file_format::encode_header(header, encoded);
    const bool wrote_header = encoded_header && descriptor_write_all(descriptor_, encoded, io_error);
    const bool synced_header = wrote_header && ::fdatasync(descriptor_) == 0;
    if (!synced_header) {
        const int failure = !encoded_header ? EINVAL : (io_error != 0 ? io_error : errno);
        ::close(descriptor_);
        descriptor_ = -1;
        const auto partial_leaf = partial_path_.filename().string();
        static_cast<void>(::unlinkat(directory_descriptor_, partial_leaf.c_str(), 0));
        ::close(directory_descriptor_);
        directory_descriptor_ = -1;
        fail(FatalCode::storage_write, "cannot persist recording header: " +
                                           errno_message(failure));
        return false;
    }
    if (storage_config_.preallocate_bytes != 0U) {
        const int allocation = ::posix_fallocate(
            descriptor_, 0, static_cast<off_t>(storage_config_.preallocate_bytes));
        if (allocation != 0) {
            ::close(descriptor_);
            descriptor_ = -1;
            const auto partial_leaf = partial_path_.filename().string();
            static_cast<void>(::unlinkat(directory_descriptor_, partial_leaf.c_str(), 0));
            ::close(directory_descriptor_);
            directory_descriptor_ = -1;
            fail(FatalCode::storage_open, "recording preallocation failed: " +
                                              errno_message(allocation));
            return false;
        }
    }
    logical_size_ = encoded.size();
    footer_ = {};
    footer_.first_block_sequence = first.header.block_sequence;
    footer_.first_sample_sequence = first.header.first_sample_sequence;
    last_sync_monotonic_ns_ = clock_.monotonic_raw_ns();
    return true;
}

bool StorageWriter::flush_aggregate() noexcept {
    if (aggregate_.empty()) return true;
    const auto start = clock_.monotonic_raw_ns();
    int io_error = 0;
    if (!descriptor_write_all(descriptor_, aggregate_, io_error)) {
        fail(FatalCode::storage_write, "recording write failed: " + errno_message(io_error));
        return false;
    }
    const auto latency_us = (clock_.monotonic_raw_ns() - start) / 1000U;
    health_.storage_last_write_latency_us.store(latency_us, std::memory_order_relaxed);
    auto previous = health_.storage_max_write_latency_us.load(std::memory_order_relaxed);
    while (latency_us > previous &&
           !health_.storage_max_write_latency_us.compare_exchange_weak(previous, latency_us)) {}
    logical_size_ += aggregate_.size();
    health_.storage_bytes.fetch_add(aggregate_.size(), std::memory_order_relaxed);
    health_.storage_blocks.fetch_add(aggregate_blocks_, std::memory_order_relaxed);
    aggregate_.clear();
    aggregate_blocks_ = 0;
    return true;
}

bool StorageWriter::synchronize() noexcept {
    if (!flush_aggregate()) return false;
    if (::fdatasync(descriptor_) != 0) {
        fail(FatalCode::storage_sync, "fdatasync failed: " + errno_message(errno));
        return false;
    }
    last_sync_monotonic_ns_ = clock_.monotonic_raw_ns();
    update_disk_space();
    return true;
}

bool StorageWriter::append_block(const protocol::SampleBlock& block) noexcept {
    std::array<std::byte, protocol::kMaxEncodedBlockSize> encoded{};
    const auto size = protocol::encode_block(block, encoded);
    if (size == 0U) {
        fail(FatalCode::storage_write, "attempted to store an invalid block");
        return false;
    }
    if (!aggregate_.empty() && aggregate_.size() + 4U + size > storage_config_.aggregate_bytes &&
        !flush_aggregate()) return false;
    const auto offset = aggregate_.size();
    aggregate_.resize(offset + 4U + size);
    put_u32(aggregate_.data() + offset, static_cast<std::uint32_t>(size));
    std::copy_n(encoded.begin(), size, aggregate_.begin() + static_cast<std::ptrdiff_t>(offset + 4U));
    ++footer_.block_count;
    footer_.sample_count += block.header.sample_count;
    footer_.last_block_sequence = block.header.block_sequence;
    footer_.last_sample_sequence = block.header.first_sample_sequence + block.header.sample_count - 1U;
    ++aggregate_blocks_;
    const auto sync_interval = static_cast<std::uint64_t>(storage_config_.sync_interval_seconds) * 1'000'000'000ULL;
    if (clock_.monotonic_raw_ns() - last_sync_monotonic_ns_ >= sync_interval) return synchronize();
    return true;
}

bool StorageWriter::finalize_file(bool recovered) noexcept {
    if (descriptor_ < 0) return true;
    if (!flush_aggregate()) return false;
    if (::ftruncate(descriptor_, static_cast<off_t>(logical_size_)) != 0) {
        fail(FatalCode::storage_write, "cannot trim preallocated recording: " + errno_message(errno));
        return false;
    }
    if (recovered) footer_.footer_flags |= kRecoveryFooterFlag;
    std::array<std::byte, file_format::kFileFooterSize> encoded{};
    int io_error = 0;
    if (!file_format::encode_footer(footer_, encoded) || !descriptor_write_all(descriptor_, encoded, io_error)) {
        fail(FatalCode::storage_write, "cannot write recording footer");
        return false;
    }
    logical_size_ += encoded.size();
    if (::fdatasync(descriptor_) != 0) {
        fail(FatalCode::storage_sync, "final recording sync failed: " + errno_message(errno));
        return false;
    }
    if (::close(descriptor_) != 0) {
        descriptor_ = -1;
        fail(FatalCode::storage_close, "recording close failed: " + errno_message(errno));
        return false;
    }
    descriptor_ = -1;
    const auto partial_leaf = partial_path_.filename().string();
    const auto final_leaf = final_path_.filename().string();
    if (::renameat(directory_descriptor_, partial_leaf.c_str(), directory_descriptor_,
                   final_leaf.c_str()) != 0) {
        fail(FatalCode::storage_rename, "recording rename failed: " + errno_message(errno));
        return false;
    }
    if (::fsync(directory_descriptor_) != 0) {
        fail(FatalCode::storage_sync, "recording directory sync failed: " + errno_message(errno));
        return false;
    }
    if (::close(directory_descriptor_) != 0) {
        directory_descriptor_ = -1;
        fail(FatalCode::storage_close,
             "recording directory close failed: " + errno_message(errno));
        return false;
    }
    directory_descriptor_ = -1;
    aggregate_.clear();
    aggregate_blocks_ = 0;
    logical_size_ = 0;
    footer_ = {};
    return true;
}

void StorageWriter::update_disk_space() noexcept {
    struct statvfs statistics {};
    if (directory_descriptor_ >= 0 && ::fstatvfs(directory_descriptor_, &statistics) == 0) {
        const auto available = static_cast<std::uint64_t>(statistics.f_bavail) *
                               static_cast<std::uint64_t>(statistics.f_frsize);
        health_.disk_free_bytes.store(available, std::memory_order_relaxed);
    }
}

void StorageWriter::run() noexcept {
    protocol::SampleBlock block{};
    while (!acquisition_done_.load(std::memory_order_acquire) || queue_.size() != 0U) {
        if (!queue_.try_pop(block)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            continue;
        }
        health_.disk_queue_depth.store(queue_.size(), std::memory_order_relaxed);
        const auto rotation_ns = static_cast<std::uint64_t>(storage_config_.rotation_minutes) * 60ULL * 1'000'000'000ULL;
        const auto bucket = block.header.utc_start_nanoseconds / rotation_ns;
        if (descriptor_ >= 0 && bucket != rotation_bucket_ && !finalize_file()) break;
        if (descriptor_ < 0 && !open_file(block)) break;
        if (!append_block(block)) break;
    }
    if (!failed_ && descriptor_ >= 0 && footer_.block_count != 0U) {
        static_cast<void>(finalize_file());
    }
    if (failed_ && descriptor_ >= 0) {
        if (::fdatasync(descriptor_) != 0) {
            health_.storage_errors.fetch_add(1U, std::memory_order_relaxed);
        }
        if (::close(descriptor_) != 0) {
            health_.storage_errors.fetch_add(1U, std::memory_order_relaxed);
        }
        descriptor_ = -1;
    }
    if (directory_descriptor_ >= 0) {
        if (::close(directory_descriptor_) != 0) {
            health_.storage_errors.fetch_add(1U, std::memory_order_relaxed);
        }
        directory_descriptor_ = -1;
    }
}

}  // namespace shakepie
