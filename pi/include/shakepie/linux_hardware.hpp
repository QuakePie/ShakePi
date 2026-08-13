#pragma once

#if defined(__linux__)

#include "shakepie/ads1256.hpp"
#include "shakepie/config.hpp"

#include <memory>
#include <string>

namespace shakepie {

[[nodiscard]] std::unique_ptr<IAds1256Transport> make_linux_ads1256_transport(
    const AdcConfig& config, std::string& error);

}  // namespace shakepie

#endif
