# Alternative to build.sh:  cmake -S . -B build -G Ninja && ninja -C build
# Produces build/libpybinobf.so (pybin.py auto-detects that path too).
cmake_minimum_required(VERSION 3.20)
project(pybinobf LANGUAGES CXX)

find_package(LLVM REQUIRED CONFIG)
message(STATUS "Using LLVM ${LLVM_PACKAGE_VERSION} (${LLVM_DIR})")

list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)

add_definitions(${LLVM_DEFINITIONS})
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})

add_library(pybinobf MODULE Obfuscator.cpp)
set_target_properties(pybinobf PROPERTIES PREFIX "lib" OUTPUT_NAME "pybinobf")
target_compile_features(pybinobf PRIVATE cxx_std_17)

# LLVM is typically built without RTTI; match it.
if(NOT LLVM_ENABLE_RTTI)
  target_compile_options(pybinobf PRIVATE -fno-rtti)
endif()
