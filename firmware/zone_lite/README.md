# Zone Lite ESP32 Firmware

Prototype firmware for an ESP32-S3 Zone Lite gateway.

Current milestone:

- Connects to Wi-Fi.
- Scans the DHCP subnet for the first host accepting ZKT TCP port `4370`.
- Confirms the host by running ZKT `CMD_CONNECT` and Comm Key auth.
- Reads basic identity/time/count information from the selected device.
- Repeats discovery so DHCP IP changes are handled without reflashing.

Create local secrets before building:

```bash
cp main/zone_lite_config.example.h main/zone_lite_config.h
```

`main/zone_lite_config.h` is ignored by git.
