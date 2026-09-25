"""Actual predecessor guard with injected NVS/partition failures in both modes."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_compatibility_proof_precedes_segmented_activation(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    headers = r"""
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define ESP_OK 0
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define ESP_PARTITION_SUBTYPE_APP_OTA_0 16
#define ESP_PARTITION_SUBTYPE_APP_OTA_1 17
typedef int esp_err_t;
typedef int nvs_handle_t;
typedef struct { char project_name[32],version[32]; } esp_app_desc_t;
typedef struct { uint32_t address,subtype; } esp_partition_t;
const esp_app_desc_t *esp_app_get_description(void);
const esp_partition_t *esp_ota_get_running_partition(void);
const esp_partition_t *esp_ota_get_next_update_partition(const void *);
int esp_ota_get_partition_description(const esp_partition_t *,esp_app_desc_t *);
int esp_partition_get_sha256(const esp_partition_t *,uint8_t *);
bool esp_secure_boot_enabled(void);
int nvs_open(const char *,int,int *);
int nvs_get_blob(int,const char *,void *,size_t *);
int nvs_set_blob(int,const char *,const void *,size_t);
int nvs_commit(int);
void nvs_close(int);
"""
    (tmp_path / "ports.h").write_text(headers)
    for header in (
        "esp_app_desc.h",
        "esp_ota_ops.h",
        "esp_partition.h",
        "esp_secure_boot.h",
        "nvs.h",
    ):
        (tmp_path / header).write_text('#include "ports.h"\n')
    harness = r"""
#include "ports.h"
#include "storage_upgrade.h"
#include "upgrade_guard.h"
#include <assert.h>
#include <stdio.h>
static esp_app_desc_t app={"zone_lite",UG_COMPAT_VERSION},previous_app={"zone_lite",UG_COMPAT_VERSION};
static esp_partition_t current={0x20000,17},previous={0x2a0000,16};
static ug_capability_t pending,durable;
static unsigned failure,writes,commits;
static bool secure=true;
const esp_app_desc_t *esp_app_get_description(void){return &app;}
const esp_partition_t *esp_ota_get_running_partition(void){return &current;}
const esp_partition_t *esp_ota_get_next_update_partition(const void *ignored){(void)ignored;return &previous;}
int esp_ota_get_partition_description(const esp_partition_t *p,esp_app_desc_t *out){assert(p==&previous);*out=previous_app;return failure==5?-1:0;}
int esp_partition_get_sha256(const esp_partition_t *p,uint8_t *out){
    assert(p==&previous || p==&current);
#if ZONE_LITE_DIRECT_LEGACY_UPGRADE
    const char *hex=!strcmp(previous_app.version,"2.4.12")?
        "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589":
        !strcmp(previous_app.version,"2.6.6")?
        "69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e":
        "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b";
    for(unsigned i=0;i<32;++i){unsigned value=0;assert(sscanf(hex+2*i,"%2x",&value)==1);out[i]=(uint8_t)value;}
    if(failure==7)out[0]^=1;
#else
    memset(out,failure==7?0x22:0x11,32);
#endif
    return failure==6?-1:0;
}
bool esp_secure_boot_enabled(void){return secure;}
int nvs_open(const char *space,int mode,int *handle){assert(!strcmp(space,"queue_upgrade"));(void)mode;*handle=1;return failure==1?-1:0;}
int nvs_get_blob(int handle,const char *key,void *out,size_t *length){assert(handle==1 && !strcmp(key,"reader_v1") && *length==sizeof(durable));memcpy(out,&durable,sizeof(durable));return failure==4?-1:0;}
int nvs_set_blob(int handle,const char *key,const void *data,size_t length){assert(handle==1 && !strcmp(key,"reader_v1") && length==sizeof(pending));pending=*(const ug_capability_t *)data;++writes;return failure==2?-1:0;}
int nvs_commit(int handle){assert(handle==1);++commits;if(failure==3)return -1;durable=pending;return 0;}
void nvs_close(int handle){assert(handle==1);}
int main(void)
{
#if ZONE_LITE_HIKVISION
    assert(!storage_upgrade_init()); /* Reject a ZKT application. */
    strcpy(app.project_name,"zone_lite_hikvision");strcpy(app.version,"3.0.3");
    assert(storage_upgrade_init() && storage_upgrade_ready());
    assert(!storage_upgrade_segmented_writes() && !writes && !commits);
    assert(strstr(storage_upgrade_contract(),"HIKVISION_STORAGE_CONTRACT"));
    secure=false;assert(!storage_upgrade_init() && !storage_upgrade_ready());secure=true;
    assert(storage_upgrade_init());
    strcpy(app.project_name,"unknown");assert(!storage_upgrade_init());
#elif ZONE_LITE_DIRECT_LEGACY_UPGRADE
    strcpy(app.version,UG_DIRECT_VERSION);
    strcpy(previous_app.version,"2.4.12");
    assert(storage_upgrade_init() && storage_upgrade_ready() && !storage_upgrade_segmented_writes());
    assert(!writes && !commits);
    assert(strstr(storage_upgrade_contract(),"BASE=2.4.12,2.5.2"));
    strcpy(previous_app.version,"2.5.2");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.6.6");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    failure=6;assert(!storage_upgrade_init());failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,UG_COMPAT_VERSION);assert(!storage_upgrade_init());
    strcpy(previous_app.version,UG_CANDIDATE_VERSION);assert(!storage_upgrade_init());
    strcpy(previous_app.version,"2.4.12");
    failure=5;assert(!storage_upgrade_init());failure=0;
    current.subtype=0;assert(!storage_upgrade_init());current.subtype=17;
    previous.address=current.address;assert(!storage_upgrade_init());previous.address=0x2a0000;
    strcpy(app.version,UG_CANDIDATE_VERSION);assert(!storage_upgrade_init());
    strcpy(app.version,UG_DIRECT_VERSION);
    secure=false;assert(!storage_upgrade_init());secure=true;
    assert(storage_upgrade_init() && !storage_upgrade_segmented_writes());
#elif ZONE_LITE_SEGMENTED_WRITES
    FILE *file=fopen("compatibility-proof.bin","rb");assert(file);
    assert(fread(&durable,sizeof(durable),1,file)==1);assert(!fclose(file));
    strcpy(app.version,UG_CANDIDATE_VERSION);
    assert(storage_upgrade_init() && storage_upgrade_segmented_writes());
    assert(!writes && !commits);
    unsigned failures[]={1,4,5,6,7};
    for(unsigned i=0;i<sizeof(failures)/sizeof(*failures);++i){
        failure=failures[i];assert(!storage_upgrade_init());assert(!storage_upgrade_ready() && !storage_upgrade_segmented_writes() && storage_upgrade_error()[0]);
    }
    failure=0;
    strcpy(previous_app.version,"2.4.12");assert(!storage_upgrade_init());strcpy(previous_app.version,UG_COMPAT_VERSION);
    current.subtype=0;assert(!storage_upgrade_init());current.subtype=17;
    previous.address=current.address;assert(!storage_upgrade_init());previous.address=0x2a0000;
    durable.reader_mask^=1;assert(!storage_upgrade_init());durable.reader_mask^=1;
    durable.crc^=1;assert(!storage_upgrade_init());durable.crc^=1;
    strcpy(app.version,"2.4.12");assert(!storage_upgrade_init());strcpy(app.version,UG_CANDIDATE_VERSION);
    secure=false;assert(!storage_upgrade_init());secure=true;
    assert(storage_upgrade_init() && storage_upgrade_segmented_writes());
#else
    strcpy(app.version,"2.5.2");assert(storage_upgrade_init() && !writes && !storage_upgrade_segmented_writes());
    strcpy(app.version,"2.5.4");
    assert(storage_upgrade_init() && writes == 1 && commits == 1 && !storage_upgrade_segmented_writes());
    strcpy(app.version,UG_COMPAT_VERSION);
    for(failure=1;failure<=3;++failure){assert(!storage_upgrade_init() && !storage_upgrade_ready());}
    failure=0;secure=false;assert(!storage_upgrade_init());secure=true;
    assert(storage_upgrade_init() && !storage_upgrade_segmented_writes());
    assert(ug_capability_valid(&durable));
    FILE *file=fopen("compatibility-proof.bin","wb");assert(file);
    assert(fwrite(&durable,sizeof(durable),1,file)==1);assert(!fclose(file));
#endif
    puts("storage predecessor guard regressions passed");
}
"""
    unit = tmp_path / "upgrade.c"
    unit.write_text(harness)
    for mode, hikvision, direct in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
        executable = tmp_path / f"upgrade-{mode}-{hikvision}-{direct}"
        subprocess.run(
            [
                shutil.which("cc"),
                "-std=c11",
                "-D_POSIX_C_SOURCE=200809L",
                "-g",
                "-O1",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
                f"-DZONE_LITE_SEGMENTED_WRITES={mode}",
                f"-DZONE_LITE_HIKVISION={hikvision}",
                f"-DZONE_LITE_DIRECT_LEGACY_UPGRADE={direct}",
                "-I",
                str(tmp_path),
                "-I",
                str(firmware),
                str(unit),
                str(firmware / "storage_upgrade.c"),
                str(firmware / "upgrade_guard.c"),
                str(firmware / "durable_queue.c"),
                "-o",
                str(executable),
            ],
            check=True,
        )
        subprocess.run([str(executable)], cwd=tmp_path, check=True)
