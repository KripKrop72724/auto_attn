#!/usr/bin/env python3
"""Build the State Life board test program and refresh the installer's copy.

    python3 build_images.py            build, check, copy into
                                       src/sl_installer/resources/boardtest/
    python3 build_images.py --check    rebuild in a temporary folder and fail
                                       unless the bytes equal the committed files

The build always runs in the pinned ESP-IDF image (espressif/idf:v5.5.3) with
the repository mounted read-only at /project and the build folder at /tmp/bt
inside the container, so every build sees the same paths and
CONFIG_APP_REPRODUCIBLE_BUILD gives the same bytes.

Every build is refused when the final sdkconfig enables anything that can
burn eFuses or lock a board (Secure Boot, flash encryption, encrypted NVS,
secure/disabled ROM download mode, anti-rollback, aggressive key revoke, ROM
log eFuse schemes), when a required hardware option is missing, or when the
program's sources call any eFuse / Secure Boot / flash encryption API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROJECT_IN_CONTAINER = "/project/" + HERE.relative_to(REPO).as_posix()
RESOURCES = HERE.parent / "src" / "sl_installer" / "resources" / "boardtest"
IMAGE = "espressif/idf:v5.5.3"
IDF_VERSION = "v5.5.3"
VERSION = "1.0.0"
PROJECT_NAME = "board-test"
APP_PARTITION_SIZE = 0x100000

# (published name, file in the build folder, flash offset)
IMAGES = (
    ("bootloader.bin", "bootloader/bootloader.bin", 0x0),
    ("partition-table.bin", "partition_table/partition-table.bin", 0x8000),
    ("board-test.bin", "board-test.bin", 0x10000),
)

# An option whose name matches one of these must not be enabled (=y or any
# value other than n) in the final sdkconfig.
FORBIDDEN_OPTIONS = (
    r"SECURE_BOOT\w*",
    r"SECURE_FLASH_ENC\w*",
    r"SECURE_FLASH_ENCRYPTION\w*",
    r"FLASH_ENCRYPTION\w*",
    r"SECURE_FLASH_\w*",
    r"NVS_ENCRYPTION",
    r"NVS_SEC_KEY_PROTECT\w*",
    r"NVS_SEC_HMAC\w*",
    r"SECURE_ENABLE_SECURE_ROM_DL_MODE",
    r"SECURE_DISABLE_ROM_DL_MODE",
    r"SECURE_\w*ROM_DL_MODE\w*",
    r"SECURE_SIGNED\w*",
    r"SECURE_BOOT_ENABLE_AGGRESSIVE_KEY_REVOKE",
    r"BOOTLOADER_APP_ANTI_ROLLBACK",
    r"APP_ANTI_ROLLBACK",
    r"BOOTLOADER_APP_SECURE_VERSION\w*",
    r"BOOT_ROM_LOG_ALWAYS_OFF",
    r"BOOT_ROM_LOG_ON_GPIO\w*",
    r"EFUSE_VIRTUAL\w*",
    r"ESP32_DISABLE_BASIC_ROM_CONSOLE",
)
# Read-only capability flags that match the patterns above but enable
# nothing: "the chip supports / prefers this", and SECURE_ROM_DL_MODE_ENABLED,
# which only picks the default of the SECURE_UART_ROM_DL_MODE choice; that
# choice exists only with Secure Boot or flash encryption on, and the burning
# options it offers (SECURE_ENABLE_SECURE_ROM_DL_MODE,
# SECURE_DISABLE_ROM_DL_MODE) stay forbidden.
HARMLESS_OPTIONS = re.compile(r"\w+_(?:SUPPORTED|PREFERRED)|SECURE_ROM_DL_MODE_ENABLED")
# Options the final sdkconfig must contain exactly (production hardware
# handling + the board-test differences).
REQUIRED_LINES = (
    'CONFIG_IDF_TARGET="esp32s3"',
    "CONFIG_APP_REPRODUCIBLE_BUILD=y",
    "CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y",
    "CONFIG_ESPTOOLPY_FLASHMODE_DIO=y",
    "CONFIG_ESPTOOLPY_FLASHFREQ_80M=y",
    "CONFIG_SPIRAM=y",
    "CONFIG_SPIRAM_MODE_OCT=y",
    "CONFIG_SPIRAM_TYPE_AUTO=y",
    "CONFIG_SPIRAM_SPEED_80M=y",
    "CONFIG_SPIRAM_USE_MALLOC=y",
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y",
    "CONFIG_ESP32S3_REV_MIN_0=y",
    "CONFIG_BOOTLOADER_FLASH_XMC_SUPPORT=y",
    "# CONFIG_SPIRAM_MEMTEST is not set",
    "CONFIG_SPIRAM_IGNORE_NOTFOUND=y",
    "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y",
    "CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT=y",
    "CONFIG_ESP_TASK_WDT_EN=y",
    "CONFIG_ESP_TASK_WDT_PANIC=y",
    "CONFIG_ESP_TASK_WDT_TIMEOUT_S=10",
    "CONFIG_BOOTLOADER_LOG_LEVEL_INFO=y",
    "CONFIG_PARTITION_TABLE_CUSTOM=y",
    "CONFIG_PARTITION_TABLE_OFFSET=0x8000",
    "CONFIG_BOOT_ROM_LOG_ALWAYS_ON=y",
    "# CONFIG_SECURE_BOOT is not set",
    "# CONFIG_SECURE_FLASH_ENC_ENABLED is not set",
    "# CONFIG_NVS_ENCRYPTION is not set",
)
# Any of these words in the program's code (comments removed) fails the build:
# the program must not touch eFuses, Secure Boot or flash encryption at all.
FORBIDDEN_CODE = re.compile(
    r"efuse|secure_boot|flash_encrypt|flash_enc_|esp_secure|ets_secure|"
    r"key_purpose|hmac|ds_peripheral|esp_ds_", re.IGNORECASE)
COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


class BuildError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_sources() -> None:
    sources = sorted((HERE / "main").glob("*.[ch]"))
    if not sources:
        raise BuildError("no sources found in main/")
    problems = []
    for path in sources:
        code = COMMENTS.sub(" ", path.read_text(encoding="utf-8"))
        for number, line in enumerate(code.splitlines(), 1):
            if FORBIDDEN_CODE.search(line):
                problems.append(f"{path.name}:{number}: {line.strip()}")
    for path in [HERE / "CMakeLists.txt", HERE / "main" / "CMakeLists.txt"]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"--wrap|esp_panic_handler", text):
            problems.append(f"{path.name}: must not wrap the panic handler")
    if problems:
        raise BuildError("forbidden calls in the board test sources:\n  " + "\n  ".join(problems))


def check_sdkconfig(text: str) -> None:
    lines = text.splitlines()
    problems = []
    forbidden = re.compile(r"CONFIG_(?:%s)=(.*)" % "|".join(FORBIDDEN_OPTIONS))
    for line in lines:
        match = forbidden.fullmatch(line.strip())
        name = line.strip().split("=", 1)[0][len("CONFIG_"):]
        if match and match.group(1) not in ("n", "") and not HARMLESS_OPTIONS.fullmatch(name):
            problems.append(f"forbidden option enabled: {line.strip()}")
    present = {line.strip() for line in lines}
    for line in REQUIRED_LINES:
        if line not in present:
            problems.append(f"required option missing: {line}")
    if problems:
        raise BuildError("the board test sdkconfig is not safe:\n  " + "\n  ".join(problems))


def check_header(text: str) -> None:
    if "#define CONFIG_APP_REPRODUCIBLE_BUILD 1" not in text:
        raise BuildError("sdkconfig.h does not enable the reproducible build")
    for name in ("CONFIG_APP_COMPILE_TIME_DATE", "CONFIG_BOOTLOADER_COMPILE_TIME_DATE"):
        if re.search(r"#define %s\b" % name, text):
            raise BuildError(f"{name} would put the build time into the image")
    for name in ("CONFIG_SECURE_BOOT", "CONFIG_SECURE_FLASH_ENC_ENABLED", "CONFIG_NVS_ENCRYPTION"):
        if re.search(r"#define %s\b" % name, text):
            raise BuildError(f"{name} is defined in sdkconfig.h")


def check_flasher_args(args: Dict) -> None:
    files = {name.replace("\\", "/"): int(offset, 16)
             for offset, name in args.get("flash_files", {}).items()}
    for published, built, offset in IMAGES:
        if files.get(built) != offset:
            raise BuildError(f"{built} is not at {offset:#x} in flasher_args.json: {files}")
    if len(files) != len(IMAGES):
        raise BuildError(f"unexpected images in flasher_args.json: {sorted(files)}")
    settings = args.get("flash_settings", {})
    if settings != {"flash_mode": "dio", "flash_freq": "80m", "flash_size": "16MB"}:
        raise BuildError(f"unexpected flash settings: {settings}")


def check_app(app: bytes) -> None:
    if len(app) > APP_PARTITION_SIZE:
        raise BuildError(f"board-test.bin is {len(app)} bytes, more than the app partition")
    if app[0] != 0xE9:
        raise BuildError("board-test.bin is not an ESP image")
    # esp_app_desc_t sits right after the image header (24 B) and the first
    # segment header (8 B).
    desc = app[32:32 + 256]
    magic, _secure_version = struct.unpack_from("<II", desc, 0)
    if magic != 0xABCD5432:
        raise BuildError("board-test.bin has no application description")
    version = desc[16:48].split(b"\0", 1)[0].decode()
    project = desc[48:80].split(b"\0", 1)[0].decode()
    idf = desc[112:144].split(b"\0", 1)[0].decode()
    if (version, project, idf) != (VERSION, PROJECT_NAME, IDF_VERSION):
        raise BuildError(f"unexpected app identity {version!r} {project!r} {idf!r}")
    if _secure_version != 0:
        raise BuildError("board-test.bin carries a secure version (anti-rollback)")


def docker_build(out: Path, image: str) -> None:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    script = " && ".join([
        "set -e",
        "idf.py -B /tmp/bt -D SDKCONFIG=/tmp/bt/sdkconfig build",
        "cp /tmp/bt/bootloader/bootloader.bin /tmp/bt/partition_table/partition-table.bin "
        "/tmp/bt/board-test.bin /tmp/bt/sdkconfig /tmp/bt/config/sdkconfig.h "
        "/tmp/bt/flasher_args.json /out/",
        f"chown {uid}:{gid} /out/*",
        "chmod 0644 /out/*",
    ])
    command = ["docker", "run", "--rm",
               "-e", "IDF_COMPONENT_MANAGER=0",
               "-v", f"{REPO}:/project:ro",
               "-v", f"{out}:/out",
               "-w", PROJECT_IN_CONTAINER,
               image, "bash", "-lc", script]
    print("Building the board test program in", image, flush=True)
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = proc.stdout.decode("utf-8", "replace")
    (out / "build.log").write_text(log, encoding="utf-8")
    if proc.returncode != 0:
        print("\n".join(log.splitlines()[-80:]))
        raise BuildError(f"the ESP-IDF build failed (exit {proc.returncode})")
    warnings = [line for line in log.splitlines() if "/main/" in line and "warning:" in line]
    if warnings:
        raise BuildError("compiler warnings in the board test sources:\n  "
                         + "\n  ".join(warnings))
    digest = subprocess.run(["docker", "image", "inspect", image, "--format",
                             "{{json .RepoDigests}}"], stdout=subprocess.PIPE, text=True)
    print("ESP-IDF image:", digest.stdout.strip(), flush=True)


def built_files(out: Path) -> Dict[str, bytes]:
    check_sdkconfig((out / "sdkconfig").read_text(encoding="utf-8"))
    check_header((out / "sdkconfig.h").read_text(encoding="utf-8"))
    check_flasher_args(json.loads((out / "flasher_args.json").read_text(encoding="utf-8")))
    files = {name: (out / Path(built).name).read_bytes() for name, built, _ in IMAGES}
    check_app(files["board-test.bin"])
    return files


def manifest(files: Dict[str, bytes]) -> str:
    data = {
        "version": VERSION,
        "idf": IDF_VERSION,
        "files": [{"name": name, "offset": offset, "size": len(files[name]),
                   "sha256": sha256(files[name])} for name, _, offset in IMAGES],
    }
    return json.dumps(data, indent=2) + "\n"


def build(image: str) -> Dict[str, bytes]:
    check_sources()
    with tempfile.TemporaryDirectory(prefix="sl-board-test-") as temporary:
        out = Path(temporary)
        docker_build(out, image)
        return built_files(out)


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="rebuild and compare with the committed images")
    parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args(list(argv))
    try:
        files = build(args.image)
    except BuildError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    text = manifest(files)
    if args.check:
        problems: List[str] = []
        for name, data in files.items():
            committed = RESOURCES / name
            if not committed.is_file():
                problems.append(f"{name} is not committed")
            elif committed.read_bytes() != data:
                problems.append(f"{name} differs: committed {sha256(committed.read_bytes())}, "
                                f"rebuilt {sha256(data)}")
        committed_manifest = RESOURCES / "manifest.json"
        if not committed_manifest.is_file() or \
                committed_manifest.read_text(encoding="utf-8") != text:
            problems.append("manifest.json differs from the rebuilt images")
        extra = sorted(p.name for p in RESOURCES.iterdir()
                       if p.name not in {n for n, _, _ in IMAGES} | {"manifest.json"}) \
            if RESOURCES.is_dir() else []
        if extra:
            problems.append(f"unexpected files in resources/boardtest: {extra}")
        if problems:
            print("FAILED: the committed board test images are not reproducible:\n  "
                  + "\n  ".join(problems), file=sys.stderr)
            return 1
        print("Board test images reproduced byte for byte:")
    else:
        RESOURCES.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (RESOURCES / name).write_bytes(data)
        (RESOURCES / "manifest.json").write_bytes(text.encode("utf-8"))
        print("Board test images written to", RESOURCES)
    for name, _, offset in IMAGES:
        print(f"  {name:20} {offset:#08x} {len(files[name]):8} {sha256(files[name])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
