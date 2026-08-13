set(SHAKEPIE_PI_SOURCES
    src/acquisition.cpp
    src/ads1256.cpp
    src/clocks.cpp
    src/config.cpp
    src/fake_adc.cpp
    src/file_format.cpp
    src/health.cpp
    src/paced_source.cpp
)

if(UNIX AND NOT APPLE)
    list(APPEND SHAKEPIE_PI_SOURCES
        src/linux_hardware.cpp
        src/storage.cpp
        src/stream_server.cpp
    )
endif()

add_library(shakepie_pi ${SHAKEPIE_PI_SOURCES})
target_include_directories(shakepie_pi PUBLIC include)
target_link_libraries(shakepie_pi PUBLIC shakepie_protocol)
target_compile_features(shakepie_pi PUBLIC cxx_std_20)

if(UNIX AND NOT APPLE)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(GPIOD REQUIRED IMPORTED_TARGET libgpiod)
    target_link_libraries(shakepie_pi PRIVATE PkgConfig::GPIOD pthread)
    target_compile_definitions(shakepie_pi PUBLIC SHAKEPIE_LINUX=1 PRIVATE
        _FILE_OFFSET_BITS=64
        _TIME_BITS=64
    )
    if(GPIOD_VERSION VERSION_GREATER_EQUAL "2.0")
        target_compile_definitions(shakepie_pi PRIVATE SHAKEPIE_GPIOD_V2=1)
    endif()
endif()

if(MSVC)
    # C4324 reports the intentional cache-line padding around queue atomics.
    target_compile_options(shakepie_pi PRIVATE /W4 /permissive- /wd4324)
else()
    target_compile_options(shakepie_pi PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
endif()

if(UNIX AND NOT APPLE)
    add_executable(shakepie-recorder src/recorder_main.cpp)
    target_link_libraries(shakepie-recorder PRIVATE shakepie_pi)

    add_executable(shakepie-health src/health_main.cpp)
    target_link_libraries(shakepie-health PRIVATE shakepie_pi)

    install(TARGETS shakepie-recorder shakepie-health RUNTIME DESTINATION bin)
endif()

add_executable(shakepie-inspect src/inspect_main.cpp)
target_link_libraries(shakepie-inspect PRIVATE shakepie_pi)
install(TARGETS shakepie-inspect RUNTIME DESTINATION bin)

install(FILES config/recorder.conf DESTINATION share/shakepie/config)
