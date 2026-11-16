# State Life board hardware test program (ESP-IDF 5.5.3, ESP32-S3).
# UNSECURED by design: it never enables Secure Boot, flash encryption or
# encrypted NVS and never writes eFuses. build_images.py enforces this.
cmake_minimum_required(VERSION 3.16)
set(IDF_TARGET esp32s3)
set(PROJECT_VER "1.0.0")
# Only the components main needs (and what they pull in).
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Link without debug information: the prebuilt toolchain libraries carry
# build-host specific debug paths (the amd64 and arm64 toolchains differ) and
# the ELF's SHA-256 is embedded in the image. Without debug sections the image
# is byte-identical on every build host (CI runs amd64, Macs arm64).
idf_build_set_property(LINK_OPTIONS "-Wl,--strip-debug" APPEND)
project(board-test)
