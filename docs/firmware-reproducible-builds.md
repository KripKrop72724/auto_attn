# Reproducing unsigned firmware builds

Firmware defaults enable `CONFIG_APP_REPRODUCIBLE_BUILD`. ESP-IDF 5.5.3 removes
build-time metadata and maps compilation paths so identical inputs can produce
identical unsigned binaries and ELF files. Existing `sdkconfig` files must be
regenerated to adopt a changed default; CI creates fresh configurations.
[Espressif's reproducible-build documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/api-guides/reproducible-builds.html)

The firmware CI job builds the development 2.7.0 writer and 2.6.16 bridge twice,
in separate build directories at different mounted source paths and times. Each
pair uses the same checkout and resolved ESP-IDF image. Both application
descriptors must match the intended version/family. The comparison then requires
byte equality of the application binary/ELF, bootloader binary/ELF, partition
table and initial OTA data. Missing, empty, shared or different artifacts fail
the job, as does a configuration that still embeds build timestamps. The job
records the container repository digest and artifact SHA-256 values.

`scripts/check_firmware_reproducibility.py` compares the supplied outputs. It
cannot independently prove that a compiler produced them: CI's separate clean
build commands supply that provenance. The CI check covers its actual toolchain
and architecture; it does not establish equality across arbitrary compiler,
ESP-IDF, dependency or credential changes. Unsigned CI builds use the existing
non-production setup credential and are not provisionable release artifacts.

For a release, retain the exact Git SHA, resolved build-image digest and
architecture, tool versions, effective non-secret build options and input
hashes, both build results, unsigned output digests and the signing/manifest
record. Keep secret-bearing compiler commands, generated configuration and
embedded production images in protected storage. Reproduce with the same
authorized secret inputs; do not publish those inputs or a separate password
hash as build evidence.

Reproducibility compares unsigned inputs to signing. Promotion must retain the
exact approved signed bytes and their manifest identity, rather than rebuilding
or signing again for each zone. A reproducible build neither qualifies rollback
nor proves attendance behavior, resource capacity or HIL success. The current
2.6.16/2.7.0 registration guard and all field qualification requirements remain
in force.
