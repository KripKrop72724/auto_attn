# Spare-board Wi-Fi recovery regression

This is an **unsigned, test-only** ESP-IDF 5.5.3 application, restricted at
runtime to spare MAC `14:c1:9f:46:ae:1c`. It compiles the actual fleet portal
sources and assets. It never links attendance, SPIFFS, onboarding, terminal or
fleet credential code. Secure Boot and NVS encryption are disabled. Its AP
password is a disposable test value, not the protected fleet setup password.

The operator must also check the exact MAC **before** writing: the runtime MAC
check cannot prevent an operator from overwriting the wrong board's flash.
Back up and verify the original flash first. Do not use this harness on Lahore
MAC `ac:27:6e:a3:16:d4` or any board holding attendance data. Do not burn eFuses.

The two cases use an empty SSID, then `RE7` with a disposable incorrect password.
Each observes the real controller for 140 seconds (the recovery threshold is
120 seconds). The first case persists only the next scenario in the test NVS
namespace `hil_wifi` and restarts. The second leaves the AP available. It logs
the return value of `esp_wifi_connect`, disconnect events, AP state every five
seconds and the final AP SSID. Saving Wi-Fi through the HTTP portal is stubbed
to return `ESP_ERR_NOT_SUPPORTED`; this is an AP startup test only.

Build with `idf.py -B <external-build-directory> -D
SDKCONFIG=<external-build-directory>/sdkconfig build` from this directory.
Before running a second image, reset only the harness scenario key to zero or
restore the backed-up NVS bytes; do not erase the device. Retain exact source
and image hashes with both transcripts. Never label these results a passing
factory acceptance receipt: they do not test secure download, fleet signatures,
encrypted NVS, power loss, terminal binding, or attendance preservation.
