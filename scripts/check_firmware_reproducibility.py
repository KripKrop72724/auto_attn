"""Compare actual independent, unsigned ZKT builds without exposing build secrets."""
import argparse
import hashlib
import json
from pathlib import Path


ARTIFACTS = (
    "zone_lite.bin", "zone_lite.elf", "bootloader/bootloader.bin",
    "bootloader/bootloader.elf", "partition_table/partition-table.bin", "ota_data_initial.bin",
)


def compare(first: Path, second: Path) -> dict[str, str]:
    if first.resolve() == second.resolve():
        raise ValueError("INDEPENDENT_BUILD_DIRECTORIES_REQUIRED")
    for directory in (first, second):
        configuration = (directory / "config/sdkconfig.h").read_text()
        if ("#define CONFIG_APP_REPRODUCIBLE_BUILD 1\n" not in configuration
                or "#define CONFIG_APP_COMPILE_TIME_DATE " in configuration
                or "#define CONFIG_BOOTLOADER_COMPILE_TIME_DATE " in configuration):
            raise ValueError("REPRODUCIBLE_BUILD_CONFIGURATION_REQUIRED")
    evidence = {}
    for relative in ARTIFACTS:
        original, repeated = first / relative, second / relative
        if original.samefile(repeated):
            raise ValueError(f"INDEPENDENT_ARTIFACTS_REQUIRED:{relative}")
        # These fixed build artifacts are bounded by the build, not user input.
        before, after = original.read_bytes(), repeated.read_bytes()
        if not before or before != after:
            raise ValueError(f"REPRODUCIBLE_BUILD_MISMATCH:{relative}")
        evidence[relative] = hashlib.sha256(before).hexdigest()
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    arguments = parser.parse_args()
    print(json.dumps({"unsigned_artifact_sha256": compare(arguments.first, arguments.second)}, sort_keys=True))
