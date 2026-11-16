# State Life board hardware test program 1.0.0

A small, **unsecured** ESP-IDF 5.5.3 program for ESP32-S3 boards. The State
Life Device Installer writes it to a new board before any security key is
burned, reads its result lines on the USB port, and erases it again. It tests
the PSRAM, internal RAM, flash, both CPU cores and the Wi-Fi radio.

It never writes eFuses and never enables Secure Boot, flash encryption or
encrypted NVS. `build_images.py` refuses to build if the final sdkconfig enables
any such option or if the sources call any eFuse / Secure Boot / flash
encryption API. The installer also proves it on every run (eFuse summary before
== after).

## Build

```sh
python3 build_images.py           # build in espressif/idf:v5.5.3, copy to
                                  # ../src/sl_installer/resources/boardtest/
python3 build_images.py --check   # rebuild and require identical bytes (CI)
```

The repository is mounted read-only at `/project`; the build folder is
`/tmp/bt` inside the container, so every build sees the same paths and
`CONFIG_APP_REPRODUCIBLE_BUILD` gives the same bytes. The app is linked with
`-Wl,--strip-debug`: the amd64 (CI) and arm64 (Mac) variants of the image ship
toolchain libraries with different debug paths, and the ELF's SHA-256 is part
of the app image, so without this the two hosts build different bytes. Both
were checked to give identical images. No `sdkconfig` or build folder is ever
written into the repository. Build with
`--image espressif/idf@sha256:<amd64 digest>` to try another host's toolchain.

Published images (`src/sl_installer/resources/boardtest/`):

| file | flash offset |
|---|---|
| `bootloader.bin` | `0x0` |
| `partition-table.bin` | `0x8000` |
| `board-test.bin` | `0x10000` |
| `manifest.json` | `{"version", "idf", "files": [{"name", "offset" (integer), "size", "sha256"}]}` |

Partition table (`partitions.csv`): `nvs` 0x9000/0x6000, `phy_init`
0xF000/0x1000, `factory` 0x10000/0x100000, `fltest` (data, subtype 0x99)
0x200000/0xE00000. The program only writes inside `fltest` (and NVS for the
radio's calibration data); the installer erases 0x0..0x110000 afterwards.

## Hardware settings

Same as production (`firmware/zone_lite/sdkconfig.defaults` +
`sdkconfig.zkt.defaults`): ESP32-S3, 16 MB flash, DIO 80 MHz, octal PSRAM
(`SPIRAM_MODE_OCT`, `TYPE_AUTO`, 80 MHz, `USE_MALLOC`), CPU 240 MHz,
FreeRTOS 1000 Hz, minimum chip revision v0.0, XMC flash support on.

Differences: `SPIRAM_MEMTEST=n` (our own test is much stronger),
`SPIRAM_IGNORE_NOTFOUND=y` (missing PSRAM becomes a result line),
USB-Serial-JTAG primary console, `PANIC_PRINT_HALT` (one dump, then halt),
task watchdog 10 s with panic, bootloader log INFO, partition table at 0x8000,
`-O2` (`COMPILER_OPTIMIZATION_PERF`). No panic-handler wrapper.

## Line protocol (proto=1)

Every protocol line starts with `SLTEST ` and is `key=value` fields separated by
single spaces; values never contain spaces. Hex values in `addr`, `want`,
`got` and `at` are `0x` + 8 lowercase digits (`at=0` when there is no address);
`flash_id`/`id` are 6 lowercase hex digits without `0x`; `crc` is 8 lowercase
hex digits. The console ends lines with `\r\n`. Other (IDF) lines may appear
between protocol lines; IDF logging is set to WARN in `app_main`.

```
SLTEST BEGIN proto=1 fw=1.0.0 mac=aa:bb:cc:dd:ee:ff chip_rev=v0.2 cores=2 idf=v5.5.3 flash_id=c84018 flash_size=16777216 psram_size=8388608
SLTEST ALIVE stage=<psram|iram|flash|cpu|wifi> step=<n> of=<m> at=<0x........|0>
SLTEST ERR stage=<stage> test=<name> addr=0x........ want=0x........ got=0x........
SLTEST RESULT stage=<stage> result=<ok|fail|warn> code=<CODE> detail=<snake_case|number> errors=<n> ms=<n> [extra]
SLTEST END result=<ok|fail> failed=<stage,stage|none> crc=<8 hex>
SLTEST CLEAN result=<ok|fail>
```

* `BEGIN` is printed 5 s after `app_main` starts (time for the installer to
  reopen the port). `flash_size` is the chip's own JEDEC size
  (`esp_flash_get_physical_size`), not the image header's.
* `ALIVE`: one at the start of every stage (`step=0`), then at most every
  400 ms while the stage makes progress or is inside a known slow call (flash
  erase, Wi-Fi start, scan). A stage that stops making progress goes silent, so
  silence means a hang in the last `ALIVE` stage.
* `ERR`: at most 8 per stage; `errors=` in `RESULT` counts all.
* After `END` the program erases the flash blocks it used (and checks they
  read 0xFF), prints `CLEAN`, sets `RTC_CNTL_FORCE_DOWNLOAD_BOOT` and restarts:
  the chip comes back in ROM download mode on the same USB port.
* A crash prints the stock IDF panic dump and halts.

### Stages, codes and extra fields

| stage | result / code | detail | extra fields |
|---|---|---|---|
| psram | ok `OK` | `all_patterns_ok` | `size= tested= rounds=2 mb_s=` |
| psram | fail `PSRAM_NOT_FOUND` | `not_initialised` | `size=0` |
| psram | fail `PSRAM_WRONG_SIZE` | size in bytes | `size=` |
| psram | fail `PSRAM_TOO_SMALL` | largest block in bytes (< 7.5 MiB) | `size= tested=0` |
| psram | fail `PSRAM_DATA_ERRORS` | first failing test name | `size= tested= rounds=2` |
| iram | ok `OK` / fail `IRAM_DATA_ERRORS` | `all_patterns_ok` / test name / `no_block` | `tested= [mb_s=]` |
| flash | ok `OK` | `all_blocks_ok` | `id= size= blocks=14` |
| flash | fail `FLASH_ID_CHANGED` | `id_read_differs` | same |
| flash | fail `FLASH_WRONG_SIZE` | `size_not_16mb` | same |
| flash | fail `FLASH_TEST_AREA_MISSING` | `no_fltest_partition` | same |
| flash | fail `FLASH_READ_UNSTABLE` | `crc_differs` / `read_error` | same |
| flash | fail `FLASH_WRITE_ERRORS` | `erase` / `write` / `erase2` | same |
| cpu | ok `OK` / fail `CPU_MISMATCH` | `cores_agree` / `core0` / `core1` | `temp_c=<x.y or na>` (+ `sum=` on ok) |
| wifi | ok `OK` | `scan_ok` | `networks= best_rssi=` |
| wifi | warn `WIFI_NO_NETWORKS` | `0` | `networks=0` |
| wifi | fail `WIFI_RADIO_FAILED` | `<step>_<esp_err_name>`, e.g. `init_esp_err_no_mem`, `scan_esp_err_timeout` | — |

Memory test names (`test=`): `zeros ones x55 xaa addr naddr lfsr1 lfsr2 bytes
dual`. Every word of the block is written then read back (PSRAM: cache written
back and invalidated before each check). `dual`: core 0 and core 1 each write
one half at the same time, then each checks the half the other core wrote.
PSRAM: largest free PSRAM block (≥ 7.5 MiB), 2 rounds (`of=20`). Internal RAM:
largest free internal block less 24 KiB, 1 round (`of=10`). `mb_s` is MiB/s of
data written + read. Flash ERR `test=` names: `id size read erase write erase2`
(an esp_flash API error is reported as `want=0x00000000 got=<esp_err_t>`).

### END crc

The CRC-32 of every `SLTEST` line printed before `END` (`BEGIN`, `ALIVE`,
`ERR`, `RESULT`), in the order printed, each line's exact text from `SLTEST`
to the last character before the line end (no `\r`, no `\n`), joined with a
single `\n` (none after the last line). `CLEAN` comes after `END` and is not
included.

The firmware computes it with `esp_rom_crc32_le(0, ...)`, chained line by line
(`crc = esp_rom_crc32_le(crc, "\n", 1); crc = esp_rom_crc32_le(crc, line, len)`).
`esp_rom_crc32_le` inverts the value on entry and exit (ESP-IDF
`components/esp_rom/include/esp_rom_crc.h`; the reference implementation in
`components/esp_rom/linux/esp_rom_crc.c` and `patches/esp_rom_crc.c` is
`crc = ~crc; table loop (reflected 0xEDB88320); return ~crc`), so it is the
standard CRC-32 (zlib / IEEE 802.3) and chaining continues it. ESP-IDF itself
relies on this: the bootloader's `esp_rom_crc32_le(UINT32_MAX, seq, 4)` and
`otatool.py`'s `binascii.crc32(seq, 0xFFFFFFFF)` must agree. Python:

```python
import zlib
crc = "%08x" % (zlib.crc32("\n".join(lines).encode("ascii")) & 0xFFFFFFFF)
```
