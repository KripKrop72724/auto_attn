# Zone Lite ESP32 Firmware

ESP32-S3 firmware for a Zone Lite attendance gateway. The board connects to the configured Wi-Fi network, discovers an authenticated ZKT device on TCP port 4370, captures attendance, keeps a local SPIFFS outbox, and sends signed HTTPS batches directly to Oracle ORDS.

## Runtime behavior

- Auto-connects to Wi-Fi.
- Tries the configured preferred ZKT IP first, then scans the local subnet for the first authenticated ZKT device on port 4370.
- Loads the ZKT user table and applies Zone identity parsing:
  - `Name-13digitCNIC` sends a normal punch.
  - `Name-S-13digitCNIC` sends `raw_punch: "T"`.
  - Missing or invalid CNIC rows are blocked locally.
- On startup, reads the ZKT attendance dump but enqueues only records from the ZKT device's current local month.
- Sends live punch events immediately.
- Runs a 1-minute reconcile dump, also filtered to the current local month, so missed live punches are recovered without replaying old history.
- Deduplicates by Zone-compatible `event_uid`.
- Persists unsynced rows in SPIFFS and retries after power, Wi-Fi, or ORDS outages.

## Local config

Create `main/zone_lite_config.h` from `main/zone_lite_config.example.h`. The local config is intentionally ignored by Git because it contains Wi-Fi, ZKT, and ORDS credentials.

## Build and flash

```sh
. ~/esp/esp-idf/export.sh
idf.py -B build -DIDF_TARGET=esp32s3 build
idf.py -B build -p /dev/cu.usbmodem101 flash
```

To clear only the durable outbox/storage partition:

```sh
. ~/esp/esp-idf/export.sh
python "$IDF_PATH/components/partition_table/parttool.py" \
  --port /dev/cu.usbmodem101 \
  --partition-table-file build/partition_table/partition-table.bin \
  erase_partition --partition-name storage
```
