cmake_minimum_required(VERSION 3.20)

project(ShakePie VERSION 0.1.0 LANGUAGES CXX)

include(CTest)

option(SHAKEPIE_BUILD_PI "Build Raspberry Pi recorder components" ${UNIX})
option(SHAKEPIE_BUILD_TESTS "Build hardware-independent tests" ${BUILD_TESTING})
option(SHAKEPIE_ENABLE_SANITIZERS "Enable ASan and UBSan where supported" OFF)
option(SHAKEPIE_ENABLE_CLANG_TIDY "Run clang-tidy during compilation" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(SHAKEPIE_ENABLE_CLANG_TIDY)
    find_program(SHAKEPIE_CLANG_TIDY clang-tidy REQUIRED)
    set(CMAKE_CXX_CLANG_TIDY "${SHAKEPIE_CLANG_TIDY}")
endif()

add_library(shakepie_protocol
    protocol/src/protocol.cpp
)
target_include_directories(shakepie_protocol PUBLIC protocol/include)
target_compile_features(shakepie_protocol PUBLIC cxx_std_20)

if(MSVC)
    target_compile_options(shakepie_protocol PRIVATE /W4 /permissive-)
else()
    target_compile_options(shakepie_protocol PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
endif()

if(SHAKEPIE_ENABLE_SANITIZERS AND NOT MSVC)
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
endif()

if(SHAKEPIE_BUILD_PI)
    add_subdirectory(pi)
endif()

if(SHAKEPIE_BUILD_TESTS)
    add_subdirectory(tests/cpp)
endif()

install(FILES LICENSE README.md protocol/FORMAT.md protocol/PROTOCOL.md
        DESTINATION share/doc/shakepie)
install(DIRECTORY docs/ DESTINATION share/doc/shakepie)
