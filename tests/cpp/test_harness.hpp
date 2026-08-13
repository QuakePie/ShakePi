#pragma once

#include <iostream>

inline int shakepie_test_failures = 0;

inline void shakepie_check(bool condition, const char* expression, const char* file, int line) {
    if (!condition) {
        std::cerr << file << ':' << line << ": check failed: " << expression << '\n';
        ++shakepie_test_failures;
    }
}

#define CHECK(expression) shakepie_check((expression), #expression, __FILE__, __LINE__)
