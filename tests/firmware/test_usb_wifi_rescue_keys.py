import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "firmware/zone_lite/usb_wifi_rescue/main"


def test_wifi_only_repair_preserves_other_keys_and_recovers_each_failure(tmp_path):
    (tmp_path / "nvs.h").write_text(textwrap.dedent("""
        #pragma once
        #include <stddef.h>
        typedef int nvs_handle_t;
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL -1
        #define ESP_ERR_INVALID_ARG -2
        #define ESP_ERR_NVS_NOT_FOUND -3
        esp_err_t nvs_get_str(nvs_handle_t, const char *, char *, size_t *);
        esp_err_t nvs_set_str(nvs_handle_t, const char *, const char *);
        esp_err_t nvs_commit(nvs_handle_t);
    """))
    harness = tmp_path / "test.c"
    harness.write_text(textwrap.dedent(r"""
        #include <assert.h>
        #include <stdbool.h>
        #include <stdio.h>
        #include <string.h>
        #include "wifi_keys.h"
        static char ssid[80], password[80], protected_key[80];
        static int step, failure, writes;
        static bool missing, corrupt_readback, replacing;
        static bool fail(void) { return ++step == failure; }
        int nvs_get_str(int h, const char *key, char *out, size_t *size) {
            assert(h == 123);
            if (fail()) return ESP_FAIL;
            assert(!strcmp(key,"wifi_ssid") || !strcmp(key,"wifi_pass"));
            if (missing && !strcmp(key,"wifi_ssid")) return ESP_ERR_NVS_NOT_FOUND;
            const char *s = !strcmp(key,"wifi_ssid") ? ssid : password;
            if (corrupt_readback && writes && !strcmp(key,"wifi_pass")) s="different";
            assert(*size >= strlen(s)+1); strcpy(out,s); *size=strlen(s)+1;
            return ESP_OK;
        }
        int nvs_set_str(int h, const char *key, const char *value) {
            assert(h == 123);
            if (fail()) return ESP_FAIL;
            /* Any accidental write outside the pair aborts the test. */
            assert(!strcmp(key,"wifi_ssid") || !strcmp(key,"wifi_pass"));
            if (!strcmp(key,"wifi_pass")) {
                assert(!ssid[0] || replacing); strcpy(password,value);
            } else {
                assert(!strcmp(password,"HIL-Recovery-Only")); strcpy(ssid,value); missing=false;
            }
            ++writes; return ESP_OK;
        }
        int nvs_commit(int h) { assert(h==123); return fail() ? ESP_FAIL : ESP_OK; }
        static void reset(void) {
            strcpy(ssid,""); strcpy(password,"old-password");
            strcpy(protected_key,"fixture-connector-token");
            step=failure=writes=0; missing=corrupt_readback=replacing=false;
        }
        int main(void) {
            bool changed;
            reset();
            assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)==ESP_OK && changed);
            assert(writes==2 && !strcmp(ssid,"RE7"));
            assert(!strcmp(protected_key,"fixture-connector-token"));
            /* Every NVS call may fail; a second attempt safely resumes. */
            for (int cut=1; cut<=6; ++cut) {
                reset(); failure=cut;
                assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)!=ESP_OK);
                assert(!changed && !strcmp(protected_key,"fixture-connector-token"));
                failure=0;
                assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)==ESP_OK);
                assert(!strcmp(ssid,"RE7") && !strcmp(password,"HIL-Recovery-Only"));
            }
            reset(); missing=true;
            assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)==ESP_OK && changed);
            reset(); strcpy(ssid,"Operator corrected this");
            assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)==ESP_OK);
            assert(!changed && !writes && !strcmp(password,"old-password"));
            reset();
            assert(rescue_wifi_keys(123,"","HIL-Recovery-Only",&changed)==ESP_ERR_INVALID_ARG);
            assert(rescue_wifi_keys(123,"RE7","short",&changed)==ESP_ERR_INVALID_ARG);
            assert(!writes);
            reset(); corrupt_readback=true;
            assert(rescue_wifi_keys(123,"RE7","HIL-Recovery-Only",&changed)==ESP_FAIL && !changed);
            /* Direct mode is explicitly authorized to replace this pair only.
             * Nonempty old SSID, changed password, every failed call and retry. */
            for (int cut=0; cut<=6; ++cut) {
                reset(); replacing=true; strcpy(ssid,"Old site"); failure=cut;
                int result=rescue_wifi_keys_exact(123,"Admin Department (H&AI)","HIL-Recovery-Only",&changed);
                assert(cut ? result!=ESP_OK : result==ESP_OK);
                assert(!strcmp(protected_key,"fixture-connector-token"));
                failure=0;
                assert(rescue_wifi_keys_exact(123,"Admin Department (H&AI)","HIL-Recovery-Only",&changed)==ESP_OK);
                assert(!strcmp(ssid,"Admin Department (H&AI)") && !strcmp(password,"HIL-Recovery-Only"));
                writes=0;
                assert(rescue_wifi_keys_exact(123,"Admin Department (H&AI)","HIL-Recovery-Only",&changed)==ESP_OK && !changed && !writes);
                strcpy(password,"wrong-password"); writes=step=0;
                assert(rescue_wifi_keys_exact(123,"Admin Department (H&AI)","HIL-Recovery-Only",&changed)==ESP_OK && changed);
                assert(!strcmp(password,"HIL-Recovery-Only"));
            }
            puts("PASS: two-key confinement, every failure, retry, readback and operator edits");
            return 0;
        }
    """))
    exe = tmp_path / "test"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
                    "-I", str(tmp_path), "-I", str(SOURCE), str(harness), str(SOURCE / "wifi_keys.c"),
                    "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
