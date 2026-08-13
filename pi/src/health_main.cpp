#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>

int main(int argc, char** argv) {
    std::filesystem::path path = "/run/shakepie/health.json";
    if (argc == 3 && std::string(argv[1]) == "--file") path = argv[2];
    else if (argc != 1) {
        std::cerr << "usage: shakepie-health [--file PATH]\n";
        return 64;
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        std::cerr << "cannot read health file: " << path << '\n';
        return 2;
    }
    const std::string content((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    std::cout << content;
    if (content.find("\"state\": \"fatal\"") != std::string::npos) return 1;
    return 0;
}
