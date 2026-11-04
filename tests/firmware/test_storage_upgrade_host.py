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
#define ESP_ERR_NOT_FOUND 0x105
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define ESP_PARTITION_SUBTYPE_APP_OTA_0 16
#define ESP_PARTITION_SUBTYPE_APP_OTA_1 17
typedef int esp_err_t;
typedef int nvs_handle_t;
typedef struct { char project_name[32],version[32]; } esp_app_desc_t;
typedef struct { uint32_t address,subtype; } esp_partition_t;
typedef enum { ESP_OTA_IMG_NEW = 0x0U, ESP_OTA_IMG_PENDING_VERIFY = 0x1U, ESP_OTA_IMG_VALID = 0x2U,
               ESP_OTA_IMG_INVALID = 0x3U, ESP_OTA_IMG_ABORTED = 0x4U,
               ESP_OTA_IMG_UNDEFINED = 0xFFFFFFFFU } esp_ota_img_states_t;
int esp_ota_get_state_partition(const esp_partition_t *,esp_ota_img_states_t *);
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
#include "zkt_journal_compat.h"
#include "upgrade_guard.h"
#include <assert.h>
#include <stdio.h>
static esp_app_desc_t app={"zone_lite",UG_COMPAT_VERSION},previous_app={"zone_lite",UG_COMPAT_VERSION};
static esp_partition_t current={0x20000,17},previous={0x2a0000,16};
static ug_capability_t pending,durable;
static unsigned failure,writes,commits;
static bool secure=true;
/* otadata as esp_ota_get_state_partition reads it: an IDF state, or no
 * entry (ESP_ERR_NOT_FOUND) or a read error. Both slots VALID by default, so
 * every pre-2.8.1 assertion keeps its strict meaning. */
#define NO_ENTRY (-1L)
#define READ_ERROR (-2L)
static long running_state=ESP_OTA_IMG_VALID,other_state=ESP_OTA_IMG_VALID;
int esp_ota_get_state_partition(const esp_partition_t *p,esp_ota_img_states_t *out){
    assert(p==&previous || p==&current);
    long state=p==&current?running_state:other_state;
    if(state==NO_ENTRY)return ESP_ERR_NOT_FOUND;
    if(state==READ_ERROR)return -1;
    *out=(esp_ota_img_states_t)(unsigned long)state;
    return ESP_OK;
}
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
        !strcmp(previous_app.version,"2.6.12")?
        "3f9028126c8dde9816486783a27a9802ed41179cd9f8caf9935deb79a0057fc1":
        !strcmp(previous_app.version,"2.6.13")?
        "c7be4171153333563f9ed6e43f91376d42dfb9d3ca2faefda2ca6d1ac87b2def":
        !strcmp(previous_app.version,"2.6.14")?
        "7d0381fc68a01b34ab6989dfed2b5a1a93f39c293bfd0b7db1734b0871fa6469":
        !strcmp(previous_app.version,"2.6.15")?
        "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6":
        !strcmp(previous_app.version,"2.7.2") || !strcmp(previous_app.version,"2.7.1")?
        "e908496e0695d8c29e96a6f2db3840060eeab526c4012e29a3deef826f7c8eea":
        !strcmp(previous_app.version,"2.7.3")?
        "98855248bb4325103b5253ab4b15f4e939adf3ace89c208d463b09ec9ca21a6a":
        !strcmp(previous_app.version,"2.7.4")?
        "3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac":
        !strcmp(previous_app.version,"2.7.5")?
        "430e2daf29dd99ff006a476ff584e4201493d661cee67e4b4f9ba15545b17a40":
        !strcmp(previous_app.version,"2.7.6")?
        "9d7050768f1cb716e9bc11f9b7b4d20b423b3eccbe1531809130457998e96f0c":
        !strcmp(previous_app.version,"2.7.7")?
        "6d9ee63dd7e5917c83a3ada14685405c0081073d593a7901487eb2a3fd2c3bc1":
        !strcmp(previous_app.version,"2.7.8")?
        "b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e":
        !strcmp(previous_app.version,"2.8.0")?
        "21ac81b292b463cf7fa1bf0d6ce364a49493951836ad962a19dcae049f40ed76":
        !strcmp(previous_app.version,"2.8.1")?
        "21238fa36df844642545e8c2f6bcfec03be37470890b776a600c39c0cdd1f5f0":
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
#elif ZONE_LITE_JOURNAL_BRIDGE_IMAGE
    strcpy(app.version,ZJ_BRIDGE_VERSION);
    assert(storage_upgrade_init() && storage_upgrade_ready());
    assert(!storage_upgrade_segmented_writes() && !writes && !commits);
    assert(!strcmp(storage_upgrade_contract(),
        "ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1"
#if ZONE_LITE_JOURNAL_REPLACEMENT_BRIDGE
        ":VERSION=" ZJ_BRIDGE_VERSION
#endif
        ));
    secure=false;assert(!storage_upgrade_init() && !storage_upgrade_ready());secure=true;
    strcpy(app.version,"2.7.0");assert(!storage_upgrade_init());
    strcpy(app.version,"2.6.15");assert(!storage_upgrade_init());
    strcpy(app.version,ZJ_BRIDGE_VERSION);assert(storage_upgrade_init());
    strcpy(app.project_name,"unknown");assert(!storage_upgrade_init());
#elif ZONE_LITE_DIRECT_LEGACY_UPGRADE
    strcpy(app.version,UG_DIRECT_VERSION);
    strcpy(previous_app.version,"2.4.12");
    assert(storage_upgrade_init() && storage_upgrade_ready() && !storage_upgrade_segmented_writes());
    assert(!writes && !commits);
    assert(strstr(storage_upgrade_contract(),"BASE=2.4.12,2.5.2"));
    strcpy(previous_app.version,"2.5.2");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.6.6");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.6.12");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.6.13");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.6.14");assert(storage_upgrade_init());
    /* The 2.7 platform rolls back to the fleet's exact signed 2.6.15. */
    strcpy(previous_app.version,"2.6.15");assert(storage_upgrade_init());
    assert(!strcmp(UG_DIRECT_VERSION,"2.8.2"));
    assert(!strcmp(strstr(storage_upgrade_contract(),",2.6.15,"),",2.6.15,2.7.2,2.7.3,2.7.4,2.7.5,2.7.6,2.7.7,2.7.8,2.8.0,2.8.1"));
    /* The marker is 178 characters; ADD's heartbeat field keeps 256. */
    assert(strlen(storage_upgrade_contract())==178);
    assert(!strcmp(storage_upgrade_rollback_slot(),"PREDECESSOR") &&
           !strcmp(storage_upgrade_rollback_slot_state(),"VALID"));
    /* 2.8.2 also rolls back to 2.7.2 (3FL), 2.7.3, 2.7.4, 2.7.5, 2.7.6 and
     * 2.7.7 (their HIL devices), 2.7.8 (production since 29 October), 2.8.0
     * (the secure-pull image) and 2.8.1 (AVAILABLE since 4 November), and to
     * no other 2.7 or 2.8 image: 2.7.1 with 2.7.2's digest is still refused,
     * so are 2.7.5 to 2.8.1 on other bytes, and so is a 2.8.2 in the other
     * slot while it is selectable. */
    strcpy(previous_app.version,"2.7.2");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.7.3");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.7.4");assert(storage_upgrade_init());
    strcpy(previous_app.version,"2.7.5");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.7.6");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.7.7");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.7.8");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.8.0");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.8.1");assert(storage_upgrade_init());
    failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,"2.7.1");assert(!storage_upgrade_init());
    strcpy(previous_app.version,"2.8.2");assert(!storage_upgrade_init());
    assert(!strcmp(storage_upgrade_rollback_slot(),"UNQUALIFIED"));
    strcpy(previous_app.version,"2.6.15");
    failure=7;assert(!storage_upgrade_init());failure=0;
    failure=6;assert(!storage_upgrade_init());failure=7;assert(!storage_upgrade_init());failure=0;
    strcpy(previous_app.version,UG_COMPAT_VERSION);assert(!storage_upgrade_init());
    strcpy(previous_app.version,UG_CANDIDATE_VERSION);assert(!storage_upgrade_init());
    strcpy(previous_app.version,"2.4.12");
    failure=5;assert(!storage_upgrade_init());failure=0;
    current.subtype=0;assert(!storage_upgrade_init());current.subtype=17;
    previous.address=current.address;assert(!storage_upgrade_init());previous.address=0x2a0000;
    strcpy(app.version,UG_CANDIDATE_VERSION);assert(!storage_upgrade_init());
    /* This build runs only as 2.8.2, never under 2.8.1's or 2.8.0's descriptor. */
    strcpy(app.version,"2.8.1");assert(!storage_upgrade_init());
    strcpy(app.version,"2.8.0");assert(!storage_upgrade_init());
    strcpy(app.version,UG_DIRECT_VERSION);
    secure=false;assert(!storage_upgrade_init());secure=true;
    assert(storage_upgrade_init() && !storage_upgrade_segmented_writes());
    /* ZKT 2.8.1: a VALID image whose other slot the bootloader never selects
     * has no rollback target. After a rollback (INVALID: the image marked
     * itself; ABORTED: a reset while it was unconfirmed) or an interrupted
     * download (esp_ota_begin erased the entry) the slot's bytes do not
     * matter: an intact newer image, a partial one (no digest), none. */
    {
        static const long nonselectable[]={NO_ENTRY,ESP_OTA_IMG_INVALID,ESP_OTA_IMG_ABORTED,ESP_OTA_IMG_PENDING_VERIFY};
        static const char *const verdicts[]={"NONE_NO_ENTRY","NONE_INVALID","NONE_ABORTED","NONE_PENDING_VERIFY"};
        static const char *const states[]={"NO_ENTRY","INVALID","ABORTED","PENDING_VERIFY"};
        for(unsigned i=0;i<4;++i){
            other_state=nonselectable[i];
            for(unsigned bytes=0;bytes<3;++bytes){
                strcpy(previous_app.version,bytes==0?"2.8.0":"2.9.9");
                failure=bytes==0?7u:bytes==1?6u:5u;
                assert(storage_upgrade_init() && storage_upgrade_ready() && !storage_upgrade_error()[0]);
                assert(!strcmp(storage_upgrade_rollback_slot(),verdicts[i]));
                assert(!strcmp(storage_upgrade_rollback_slot_state(),states[i]));
            }
        }
        failure=0;
        /* A slot the bootloader could boot stays strict, whatever its state
         * says about an unknown image: VALID, NEW, UNDEFINED, an unknown value
         * or an unreadable entry. */
        static const long selectable[]={ESP_OTA_IMG_VALID,ESP_OTA_IMG_NEW,ESP_OTA_IMG_UNDEFINED,9,READ_ERROR};
        static const char *const selectable_states[]={"VALID","NEW","UNDEFINED","UNKNOWN","UNKNOWN"};
        strcpy(previous_app.version,"2.9.9");
        for(unsigned i=0;i<5;++i){
            other_state=selectable[i];
            assert(!storage_upgrade_init() && !storage_upgrade_ready());
            assert(!strcmp(storage_upgrade_error(),"STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED"));
            assert(!strcmp(storage_upgrade_rollback_slot(),"UNQUALIFIED"));
            assert(!strcmp(storage_upgrade_rollback_slot_state(),selectable_states[i]));
        }
        /* An image that is not yet VALID can still roll back: never relaxed. */
        other_state=ESP_OTA_IMG_ABORTED;
        static const long unconfirmed[]={ESP_OTA_IMG_PENDING_VERIFY,ESP_OTA_IMG_NEW,ESP_OTA_IMG_UNDEFINED,
                                         ESP_OTA_IMG_INVALID,ESP_OTA_IMG_ABORTED,9,NO_ENTRY,READ_ERROR};
        for(unsigned i=0;i<8;++i){
            running_state=unconfirmed[i];
            assert(!storage_upgrade_init() && !strcmp(storage_upgrade_rollback_slot(),"UNQUALIFIED"));
        }
        running_state=ESP_OTA_IMG_VALID;
        /* Exhaustive: ready exactly when 2.8.0 was (a qualified predecessor,
         * in any state) or this image is VALID and the other slot is never
         * selected. Nothing 2.8.0 accepted is refused. */
        static const long all[]={NO_ENTRY,READ_ERROR,ESP_OTA_IMG_NEW,ESP_OTA_IMG_PENDING_VERIFY,ESP_OTA_IMG_VALID,
                                 ESP_OTA_IMG_INVALID,ESP_OTA_IMG_ABORTED,ESP_OTA_IMG_UNDEFINED,9};
        for(unsigned qualified=0;qualified<2;++qualified){
            for(unsigned r=0;r<9;++r){
                for(unsigned o=0;o<9;++o){
                    strcpy(previous_app.version,qualified?"2.7.8":"2.9.9");
                    running_state=all[r];other_state=all[o];
                    bool none=all[r]==ESP_OTA_IMG_VALID && (all[o]==NO_ENTRY || all[o]==ESP_OTA_IMG_INVALID ||
                                                           all[o]==ESP_OTA_IMG_ABORTED || all[o]==ESP_OTA_IMG_PENDING_VERIFY);
                    assert(storage_upgrade_init()==(qualified || none));
                    const char *none_name=all[o]==NO_ENTRY?"NONE_NO_ENTRY":all[o]==ESP_OTA_IMG_INVALID?"NONE_INVALID":
                        all[o]==ESP_OTA_IMG_ABORTED?"NONE_ABORTED":"NONE_PENDING_VERIFY";
                    assert(!strcmp(storage_upgrade_rollback_slot(),qualified?"PREDECESSOR":none?none_name:"UNQUALIFIED"));
                }
            }
        }
        running_state=other_state=ESP_OTA_IMG_VALID;
        /* Not two distinct OTA slots: refused in every state, slot unjudged. */
        other_state=ESP_OTA_IMG_ABORTED;strcpy(previous_app.version,"2.9.9");
        current.subtype=0;assert(!storage_upgrade_init() && !storage_upgrade_rollback_slot()[0]);current.subtype=17;
        previous.address=current.address;assert(!storage_upgrade_init() && !storage_upgrade_rollback_slot()[0]);
        previous.address=0x2a0000;
        other_state=ESP_OTA_IMG_VALID;strcpy(previous_app.version,"2.4.12");
        assert(storage_upgrade_init());
    }
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
    assert(!storage_upgrade_rollback_slot()[0] && !storage_upgrade_rollback_slot_state()[0]);
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
    cases = [(mode, hikvision, direct, 0, "2.6.16") for mode, hikvision, direct in
             ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))]
    cases.extend((0, 0, 0, 1, version) for version in ("2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.23"))
    for mode, hikvision, direct, bridge, version in cases:
        executable = tmp_path / f"upgrade-{mode}-{hikvision}-{direct}-{bridge}-{version}"
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
                f"-DZONE_LITE_JOURNAL_BRIDGE_IMAGE={bridge}",
                f'-DZJ_BRIDGE_VERSION="{version}"',
                f"-DZONE_LITE_JOURNAL_REPLACEMENT_BRIDGE={int(bridge and version != '2.6.16')}",
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
