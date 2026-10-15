from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def compile_run(tmp_path, sources):
    executable = tmp_path / "discovery"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_DEFAULT_SOURCE", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-I", str(tmp_path), "-I", str(MAIN), *map(str, sources),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)


def test_sadp_serial_subnet_correlation_and_malformed_packets(tmp_path):
    compile_run(tmp_path, [MAIN / "hikvision_discovery.c",
                          ROOT / "tests/firmware/hikvision_discovery_host.c"])


def test_verified_ip_is_committed_before_becoming_active(tmp_path):
    (tmp_path / "esp_err.h").write_text("typedef int esp_err_t;\n")
    source = (MAIN / "zone_config.c").read_text()
    source = source[source.index("static void copy_default("):source.index("static void read_string(")]
    harness = r'''
#include "zone_config.h"
#include "hikvision_discovery.h"
#include <assert.h>
#include <string.h>
#define strlcpy test_strlcpy
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG -2
#define NVS_READWRITE 1
#define ZONE_LITE_HIKVISION 1
typedef int nvs_handle_t;
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)(x))
#define portEXIT_CRITICAL(x) ((void)(x))
static zone_config_t s_config={.provisioned=true,.firmware_family="hikvision",.hik_host="192.168.1.200",.hik_password="retained",.hik_expected_serial="G04600476"};
static int failure, writes, commits, closes;
static char persisted[16], pending[16];
static size_t strlcpy(char *d,const char *s,size_t n){size_t z=strlen(s);if(n){size_t c=z<n-1?z:n-1;memcpy(d,s,c);d[c]=0;}return z;}
static int nvs_open(const char *ns,int mode,nvs_handle_t *h){assert(!strcmp(ns,"zone_cfg")&&mode==1);*h=1;return failure==1?-1:0;}
static int nvs_set_str(int h,const char *key,const char *value){(void)h;assert(!strcmp(key,"hik_host"));writes++;strlcpy(pending,value,sizeof(pending));return failure==2?-1:0;}
static int nvs_commit(int h){(void)h;commits++;if(failure==3)return -1;strlcpy(persisted,pending,sizeof(persisted));return 0;}
static int nvs_get_str(int h,const char *key,char *value,size_t *length){(void)h;assert(!strcmp(key,"hik_host")&&*length==16);strlcpy(value,failure==5?"192.168.1.222":persisted,*length);return failure==4?-1:0;}
static void nvs_close(int h){assert(h==1);closes++;}
/* PRODUCTION */
int main(void){
    zone_config_t before=s_config;
    for(failure=1;failure<=5;failure++){
        assert(zone_config_save_hik_host("192.168.1.201")!=ESP_OK);
        assert(!memcmp(&s_config,&before,sizeof(before)));
    }
    failure=0;int previous_writes=writes;
    assert(zone_config_save_hik_host("192.168.1.200")==ESP_OK&&writes==previous_writes);
    assert(zone_config_save_hik_host("invalid")==ESP_ERR_INVALID_ARG&&writes==previous_writes);
    assert(zone_config_save_hik_host(NULL)==ESP_ERR_INVALID_ARG);
    s_config.provisioned=false;assert(zone_config_save_hik_host("192.168.1.201")==ESP_ERR_INVALID_ARG);s_config.provisioned=true;
    assert(zone_config_save_hik_host("192.168.1.201")==ESP_OK);
    assert(!strcmp(persisted,"192.168.1.201")&&!strcmp(s_config.hik_host,persisted));
    strlcpy(before.hik_host,persisted,sizeof(before.hik_host));assert(!memcmp(&before,&s_config,sizeof(before)));
    char active[64];zone_config_copy_hik_host(active,sizeof(active));assert(!strcmp(active,persisted));
    assert(commits&&closes);return 0;
}
'''.replace("/* PRODUCTION */", source)
    (tmp_path / "nvs_test.c").write_text(harness)
    compile_run(tmp_path, [MAIN / "hikvision_discovery.c", tmp_path / "nvs_test.c"])


def test_lan_ip_change_requires_fresh_reply_and_authenticated_identity(tmp_path):
    (tmp_path / "lwip").mkdir()
    (tmp_path / "esp_err.h").write_text("#pragma once\ntypedef int esp_err_t;\n#define ESP_OK 0\n")
    (tmp_path / "esp_http_client.h").write_text("typedef enum {HTTP_METHOD_GET,HTTP_METHOD_POST,HTTP_METHOD_PUT} esp_http_client_method_t;\n")
    (tmp_path / "esp_timer.h").write_text("#include <stdint.h>\nint64_t esp_timer_get_time(void);\n")
    (tmp_path / "esp_random.h").write_text("#include <stdint.h>\nuint32_t esp_random(void);\n")
    (tmp_path / "esp_netif.h").write_text('''
#include "esp_err.h"
#include <stdint.h>
typedef int esp_netif_t;
typedef struct {uint32_t addr;} test_ip_t;
typedef struct {test_ip_t ip,netmask,gw;} esp_netif_ip_info_t;
esp_netif_t *esp_netif_get_handle_from_ifkey(const char *);
esp_err_t esp_netif_get_ip_info(esp_netif_t *,esp_netif_ip_info_t *);
''')
    (tmp_path / "lwip/sockets.h").write_text('''
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
int mock_socket(int,int,int);
int mock_setsockopt(int,int,int,const void *,socklen_t);
int mock_bind(int,const struct sockaddr *,socklen_t);
ssize_t mock_sendto(int,const void *,size_t,int,const struct sockaddr *,socklen_t);
ssize_t mock_recvfrom(int,void *,size_t,int,struct sockaddr *,socklen_t *);
int mock_close(int);
#define socket mock_socket
#define setsockopt mock_setsockopt
#define bind mock_bind
#define sendto mock_sendto
#define recvfrom mock_recvfrom
#define close mock_close
''')
    compile_run(tmp_path, [MAIN / "hikvision_discovery.c", MAIN / "hikvision_discovery_net.c",
                          ROOT / "tests/firmware/hikvision_discovery_net_host.c"])
