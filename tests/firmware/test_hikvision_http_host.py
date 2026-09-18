from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_digest_retry_drains_cached_challenge_body(tmp_path):
    (tmp_path / 'esp_err.h').write_text('#pragma once\n#include <stddef.h>\nsize_t hik_test_strlcpy(char *, const char *, size_t);\ntypedef int esp_err_t;\n#define ESP_OK 0\n#define ESP_FAIL -1\n')
    (tmp_path / 'esp_timer.h').write_text('#include <stdint.h>\nint64_t esp_timer_get_time(void);\n')
    (tmp_path / 'esp_crt_bundle.h').write_text('int esp_crt_bundle_attach(void *);\n')
    (tmp_path / 'esp_http_client.h').write_text('''
#pragma once
#include <stdbool.h>
#include "esp_err.h"
typedef enum {HTTP_METHOD_GET, HTTP_METHOD_POST, HTTP_METHOD_PUT} esp_http_client_method_t;
typedef struct client *esp_http_client_handle_t;
typedef struct {int event_id; char *header_key, *header_value; void *user_data;} esp_http_client_event_t;
#define HTTP_EVENT_ON_HEADER 1
#define HTTP_AUTH_TYPE_DIGEST 2
typedef struct {const char *url, *username, *password, *cert_pem; esp_http_client_method_t method;
 int auth_type, timeout_ms, max_authorization_retries, buffer_size, buffer_size_tx;
 bool disable_auto_redirect; esp_err_t (*event_handler)(esp_http_client_event_t *); void *user_data;
 int (*crt_bundle_attach)(void *);} esp_http_client_config_t;
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *);
int esp_http_client_set_header(esp_http_client_handle_t,const char *,const char *);
int esp_http_client_open(esp_http_client_handle_t,int);
int esp_http_client_write(esp_http_client_handle_t,const char *,int);
int esp_http_client_fetch_headers(esp_http_client_handle_t);
int esp_http_client_get_status_code(esp_http_client_handle_t);
int esp_http_client_add_auth(esp_http_client_handle_t);
int esp_http_client_read(esp_http_client_handle_t,char *,int);
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t);
int esp_http_client_close(esp_http_client_handle_t);
int esp_http_client_cleanup(esp_http_client_handle_t);
''')
    main = ROOT / 'firmware/zone_lite/main'
    executable = tmp_path / 'hik-http'
    subprocess.run([shutil.which('cc'), '-std=c11', '-g', '-O1', '-Wall', '-Wextra',
                    '-Werror', '-Dstrlcpy=hik_test_strlcpy', '-fsanitize=address,undefined', '-I', str(tmp_path), '-I', str(main),
                    str(main / 'hikvision_http.c'), str(main / 'hikvision_stream.c'),
                    str(ROOT / 'tests/firmware/hikvision_http_host.c'), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)
