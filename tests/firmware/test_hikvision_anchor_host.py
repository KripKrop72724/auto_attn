import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_anchor_ignores_presentation_metadata_but_pins_event_facts(tmp_path):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    cjson = idf / "components/json/cJSON"
    crypto = idf / "components/mbedtls/mbedtls"
    if not (cjson / "cJSON.c").is_file() or not (crypto / "library/sha256.c").is_file():
        pytest.skip("Pinned ESP-IDF cJSON/mbedTLS sources are required")
    (tmp_path / "anchor_config.h").write_text("#define MBEDTLS_SHA256_C\n")
    (tmp_path / "main.c").write_text(r'''
#include "hikvision_anchor.h"
#include <stdio.h>
#include <string.h>
int main(int argc,char **argv){
    for(int a=1;a<argc;a++){
        unsigned char digest[32];
        if(!hik_anchor_digest(argv[a],strlen(argv[a]),digest)){puts("INVALID");continue;}
        for(unsigned i=0;i<32;i++)printf("%02x",digest[i]);
        puts("");
    }
    return 0;
}
''')
    main = ROOT / "firmware/zone_lite/main"
    exe = tmp_path / "anchor"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-Wno-deprecated-declarations", '-DMBEDTLS_CONFIG_FILE="anchor_config.h"',
                    "-I", str(tmp_path), "-I", str(main), "-I", str(cjson), "-I", str(crypto / "include"),
                    str(main / "hikvision_anchor.c"), str(main / "hikvision_clock.c"),
                    str(cjson / "cJSON.c"), str(crypto / "library/sha256.c"),
                    str(crypto / "library/platform_util.c"), str(tmp_path / "main.c"), "-lm", "-o", str(exe)], check=True)
    row = dict(serialNo=593441, major=5, minor=75, employeeNoString="0007",
               time="2026-10-15T08:05:52+05:00", name="Before", pictureURL="http://old/a?token=old")
    variants = [row, {**row, "name": "After", "pictureURL": "http://new/b?token=new"},
                {**row, "time": "2026-10-15T03:05:52Z"}]
    variants += [{**row, key: value} for key, value in [
        ("serialNo", 593442), ("major", 4), ("minor", 38), ("employeeNoString", "7"),
        ("time", "2026-10-15T08:05:53+05:00")]]
    encoded = [json.dumps(v, separators=(",", ":")) for v in variants]
    invalid = ["{}", encoded[0].replace('"major":5', '"major":5.5'),
               encoded[0].replace('"major":5', '"major":5,"major":6'),
               encoded[0].replace('"0007"', '"7\\n8"'),
               encoded[0].replace("2026-10-15T08:05:52+05:00", "2026-10-15T08:05:52")]
    result = subprocess.check_output([str(exe), *encoded, *invalid], text=True).splitlines()
    facts = "HIK_ANCHOR_V2\n593441\n5\n75\n0007\n1792033552"
    expected = hashlib.sha256(facts.encode()).hexdigest()
    assert result[:3] == [expected] * 3
    assert all(value != expected and value != "INVALID" for value in result[3:8])
    assert result[8:] == ["INVALID"] * len(invalid)


def test_bld1_legacy_proof_is_bound_to_exact_hardware_and_saved_record(tmp_path):
    unit = tmp_path / "proof.c"
    unit.write_text(r'''
#include "bld1_anchor_proof.h"
#include <assert.h>
int main(void){
    unsigned char mac[]={0xac,0x27,0x6e,0xa3,0x0a,0x58};
    unsigned char old[]={0xaa,0x4c,0x5d,0x97,0xc8,0x08,0xb0,0x34,0x22,0x9a,0xf3,0x90,0xea,0x9a,0x32,0xde,0xe1,0x61,0xc2,0x19,0x1a,0xca,0x68,0xe3,0x8d,0x45,0xd0,0x79,0xfe,0xb6,0xa5,0x38};
#define PROOF(m,z,d,s,e,c,v,h) bld1_anchor_proof(m,z,d,s,e,c,v,h)
#define GOOD(m,c,v,h) PROOF(m,"LF-ZONE-BLD1-01","1","G04600476","bld1-g04600476-20261014",c,v,h)
    assert(GOOD(mac,593441,1,old));
    mac[5]++;assert(!GOOD(mac,593441,1,old));mac[5]--;
    assert(!GOOD(mac,593442,1,old)&&!GOOD(mac,593441,2,old));
    old[0]^=1;assert(!GOOD(mac,593441,1,old));old[0]^=1;
    assert(!PROOF(mac,"OTHER","1","G04600476","bld1-g04600476-20261014",593441,1,old));
    assert(!PROOF(mac,"LF-ZONE-BLD1-01","2","G04600476","bld1-g04600476-20261014",593441,1,old));
    assert(!PROOF(mac,"LF-ZONE-BLD1-01","1","OTHER","bld1-g04600476-20261014",593441,1,old));
    assert(!PROOF(mac,"LF-ZONE-BLD1-01","1","G04600476","different-epoch",593441,1,old));
    return 0;
}
''')
    exe = tmp_path / "proof"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(ROOT / "firmware/zone_lite/main"),
                    str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_legacy_checkpoint_backup_is_required_and_idempotent(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/hikvision_runtime.c").read_text()
    source = source[source.index("static bool checkpoint_backup("):source.index("static const unsigned char *checkpoint_approved_anchor(")]
    unit = tmp_path / "backup.c"
    unit.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef struct {uint32_t version,serial;unsigned char binding[32],anchor[32];} poll_checkpoint_t;
typedef int nvs_handle_t;typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NVS_NOT_FOUND 2
#define NVS_READWRITE 1
static int failure,writes,commits;static bool present;
static poll_checkpoint_t backup;
static int nvs_open(const char *name,int mode,int *h){assert(!strcmp(name,"hik_poll")&&mode==1);*h=1;return failure==1?-1:0;}
static int nvs_get_blob(int h,const char *key,void *out,size_t *size){assert(h==1&&!strcmp(key,"legacy_v1")&&*size==sizeof(backup));if(failure==2)return -1;if(!present)return 2;memcpy(out,&backup,sizeof(backup));return 0;}
static int nvs_set_blob(int h,const char *key,const void *in,size_t size){assert(h==1&&!strcmp(key,"legacy_v1")&&size==sizeof(backup));writes++;if(failure==3)return -1;memcpy(&backup,in,size);return 0;}
static int nvs_commit(int h){assert(h==1);commits++;if(failure==4)return -1;present=true;return 0;}
static void nvs_close(int h){assert(h==1);}
/* PRODUCTION */
int main(void){
    poll_checkpoint_t old={.version=1,.serial=593441};memset(old.binding,7,32);memset(old.anchor,8,32);
    for(failure=1;failure<=4;failure++){present=false;assert(!checkpoint_backup(&old));}
    failure=0;present=false;assert(checkpoint_backup(&old)&&present&&!memcmp(&old,&backup,sizeof(old)));
    int before=writes;assert(checkpoint_backup(&old)&&writes==before);
    backup.binding[0]^=1;assert(!checkpoint_backup(&old)&&writes==before);
    assert(commits);return 0;
}
'''.replace("/* PRODUCTION */", source))
    exe = tmp_path / "backup"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
