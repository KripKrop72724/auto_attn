"""Execute the factory proof/first-OTA policy; no device or flash access."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def _function(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def test_actual_owner_gate_closes_start_and_drains_without_add_downgrade(tmp_path):
    owner = (MAIN / "zkt_storage_owner.c").read_text()
    functions = _function(owner, "bool zj_owner_quiesce_factory(void)") + "\n" + _function(
        owner, "bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata)")
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stddef.h>
#define ZONE_LITE_FACTORY_TRIAL_IMAGE 1
#define ZJ_AUTHORITY_LEGACY 1
typedef int zj_metadata_t;
typedef struct {bool ready;int authority;} state_t;
typedef struct {
 state_t state;
 struct {bool append_observed,quiescing,quiesced,operation_running;} health;
 struct {unsigned count;bool checkpoint_recovery_pending;} store;
 struct {unsigned running_ticket,resume_ticket;} mailbox;
 struct {unsigned append_transfer,read_transfer;} segmented;
} owner_t;
static owner_t storage,*owner;
static atomic_flag factory_start_gate=ATOMIC_FLAG_INIT;
static bool factory_shutdown,lock_available=true,inside_lock;
static unsigned starts,notifies;
static int mailbox_lock,owner_task;
static bool enter(void){if(!lock_available)return false;assert(!inside_lock);inside_lock=true;return true;}
static void xSemaphoreGive(int ignored){(void)ignored;assert(inside_lock);inside_lock=false;}
static void xTaskNotifyGive(int ignored){(void)ignored;assert(!inside_lock);++notifies;}
static int zj_state_authority(state_t *s){return s->authority;}
bool zj_owner_quiesce_factory(void);
static bool owner_start_impl(const char *p,const zj_metadata_t *m){
 (void)p;(void)m;++starts;
 /* In-flight initialization cannot be mistaken for absent/idle owner. */
 assert(!zj_owner_quiesce_factory());owner=&storage;return true;
}
/* PRODUCTION */
int main(void){
 assert(zj_owner_start("x",NULL));assert(starts==1 && owner);
 storage.state=(state_t){true,ZJ_AUTHORITY_LEGACY};storage.health.operation_running=true;
 assert(!zj_owner_quiesce_factory());assert(factory_shutdown && storage.health.quiescing && notifies==1);
 assert(!zj_owner_start("x",NULL) && starts==1);
 storage.health.operation_running=false;storage.health.quiesced=true;storage.segmented.append_transfer=1;
 assert(!zj_owner_quiesce_factory());storage.segmented.append_transfer=0;
 assert(zj_owner_quiesce_factory() && !inside_lock);
 storage.state.authority=2;assert(!zj_owner_quiesce_factory());storage.state.authority=1;
 storage.health.append_observed=true;assert(!zj_owner_quiesce_factory());storage.health.append_observed=false;
 storage.store.count=1;assert(!zj_owner_quiesce_factory());storage.store.count=0;
 storage.store.checkpoint_recovery_pending=true;assert(!zj_owner_quiesce_factory());storage.store.checkpoint_recovery_pending=false;
 storage.mailbox.resume_ticket=9;assert(!zj_owner_quiesce_factory());storage.mailbox.resume_ticket=0;
 lock_available=false;assert(!zj_owner_quiesce_factory());lock_available=true;
 owner=NULL;assert(zj_owner_quiesce_factory());assert(!zj_owner_start("x",NULL));
 assert(!inside_lock && starts==1);return 0;
}
'''.replace("/* PRODUCTION */", functions)
    source = tmp_path / "owner.c"
    source.write_text(code)
    executable = tmp_path / "owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)


def test_actual_platform_uses_verified_factory_and_never_reopens_revocation(tmp_path):
    fixture = ROOT / "tests/firmware"
    for name in ("cJSON.h", "esp_err.h", "esp_app_desc.h", "esp_cpu.h", "esp_image_format.h",
                 "esp_mac.h", "esp_ota_ops.h", "esp_partition.h", "esp_secure_boot.h",
                 "bootloader_common.h", "nvs.h", "mbedtls/sha256.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        header = tmp_path / name
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('#include "zkt_factory_platform_host.h"\n')
    executable = tmp_path / "platform"
    defines = ["ZONE_LITE_FACTORY_TRIAL_IMAGE", "CONFIG_SECURE_BOOT_V2_ENABLED",
               "CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME", "CONFIG_SECURE_SIGNED_ON_UPDATE",
               "CONFIG_NVS_ENCRYPTION", "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE"]
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    *(f"-D{name}=1" for name in defines),
                    "-Dopendir=factory_opendir", "-Dreaddir=factory_readdir", "-Dclosedir=factory_closedir",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer", "-I", str(tmp_path), "-I", str(fixture), "-I", str(MAIN),
                    str(fixture / "zkt_factory_platform_host.c"), str(MAIN / "zkt_factory_trial.c"),
                    str(MAIN / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)


def test_actual_factory_proof_and_irreversible_transitions(tmp_path):
    source = tmp_path / "factory.c"
    source.write_text(r'''
#include <assert.h>
#include <string.h>
#include "zkt_factory_trial.h"
static unsigned writes;
static int available;
static bool fail_write,fail_readback,partial;
static uint8_t saved[ZF_PROOF_BYTES];
static int read_proof(void *ctx,uint8_t *out){(void)ctx;if(available<0)return -1;if(!available)return 0;memcpy(out,saved,sizeof(saved));if(fail_readback && writes)out[80]^=1;return 1;}
static bool write_proof(void *ctx,const uint8_t *in){(void)ctx;++writes;if(fail_write)return false;memcpy(saved,in,partial?100:sizeof(saved));available=1;return true;}
static zf_proof_port_t port={read_proof,write_proof,0};
static zf_proof_t sample(unsigned target){
    zf_proof_t p={.state=ZF_VERIFIED,.target=target,.signed_image_bytes=8192,.deployment_id="11111111-2222-4333-8444-555555555555"};
    memset(p.reader_digest,0x12,32);memset(p.signed_digest,0x34,32);memset(p.layout_digest,0x56,32);
    assert(zf_digest_parse(zf_target(target)->application_sha256,p.factory_digest));return p;
}
static void reset(void){writes=0;available=0;fail_write=fail_readback=partial=false;memset(saved,0,sizeof(saved));}
int main(void){
    uint8_t encoded[ZF_PROOF_BYTES],changed[ZF_PROOF_BYTES];zf_proof_t out;
    for(unsigned i=0;i<3;++i){const zf_target_t *t=zf_target(i);assert(t);assert(zf_target_match(t->connector_id,t->mac,t->terminal_serial)==(int)i);
        uint8_t mac[6];memcpy(mac,t->mac,6);mac[5]^=1;assert(zf_target_match(t->connector_id,mac,t->terminal_serial)==-1);
        assert(zf_target_match(t->connector_id,t->mac,"OTHER-SERIAL")==-1);
        assert(zf_target_match("other-connector",t->mac,t->terminal_serial)==-1);
        zf_proof_t p=sample(i);assert(zf_proof_encode(&p,encoded));assert(zf_proof_decode(encoded,&out));assert(!memcmp(&p,&out,sizeof(p)));
        for(unsigned byte=0;byte<ZF_PROOF_BYTES;++byte){memcpy(changed,encoded,sizeof(changed));changed[byte]^=1;assert(!zf_proof_decode(changed,&out));}
    }
    assert(!zf_target(3));assert(zf_target_match(0,0,0)==-1);
    zf_proof_t p=sample(0);assert(zf_proof_encode(&p,encoded));
    reset();assert(zf_proof_create(port,&p));assert(writes==1);assert(zf_proof_create(port,&p));assert(writes==1);
    zf_proof_t other=p;other.deployment_id[0]='2';assert(!zf_proof_create(port,&other));assert(writes==1);
    assert(zf_proof_transition(port,&p,ZF_REVOKED,&out));assert(out.state==ZF_REVOKED);
    assert(!zf_proof_transition(port,&p,ZF_FALLBACK_INTENT,&out));assert(!zf_proof_transition(port,&out,ZF_FALLBACK_INTENT,&other));
    assert(!zf_proof_create(port,&p));
    reset();assert(zf_proof_create(port,&p));assert(zf_proof_transition(port,&p,ZF_FALLBACK_INTENT,&out));
    assert(!zf_proof_transition(port,&out,ZF_REVOKED,&other));assert(!zf_proof_create(port,&p));
    for(unsigned failure=0;failure<4;++failure){reset();if(failure==0)available=-1;if(failure==1)fail_write=true;if(failure==2)fail_readback=true;if(failure==3)partial=true;
        assert(!zf_proof_create(port,&p));assert(writes==(failure==0?0:1));}
    reset();assert(zf_proof_create(port,&p));fail_readback=true;assert(!zf_proof_transition(port,&p,ZF_REVOKED,&out));
    assert(writes==1); /* Stale/mismatching read prevents even the next write. */
    p=sample(0);p.factory_digest[0]^=1;assert(!zf_proof_encode(&p,encoded));
    p=sample(0);p.signed_image_bytes=ZF_FACTORY_SIZE+4096;assert(!zf_proof_encode(&p,encoded));
    p=sample(0);p.signed_image_bytes=8193;assert(!zf_proof_encode(&p,encoded));
    p=sample(0);memset(p.deployment_id,'x',sizeof(p.deployment_id));assert(!zf_proof_encode(&p,encoded));
    uint32_t seq[2]={1,UINT32_MAX},state[2]={1,UINT32_MAX};bool crc[2]={true,false};
    assert(zf_fallback_metadata(2,0,seq,state,crc));assert(!zf_fallback_metadata(2,1,seq,state,crc));
    state[0]=2;assert(!zf_fallback_metadata(2,0,seq,state,crc));state[0]=1;
    seq[1]=2;state[1]=2;crc[1]=true;assert(!zf_fallback_metadata(2,0,seq,state,crc));
    state[1]=3;assert(zf_fallback_metadata(2,0,seq,state,crc));state[1]=4;assert(zf_fallback_metadata(2,0,seq,state,crc));
    crc[0]=false;assert(!zf_fallback_metadata(2,0,seq,state,crc));crc[0]=true;
    seq[0]=0;assert(!zf_fallback_metadata(2,0,seq,state,crc));seq[0]=1;
    assert(!zf_fallback_metadata(1,0,seq,state,crc));assert(!zf_fallback_metadata(2,2,seq,state,crc));
    seq[0]=UINT32_MAX;seq[1]=2;state[1]=1;crc[0]=false;assert(zf_fallback_metadata(2,1,seq,state,crc));
    return 0;
}
''')
    executable = tmp_path / "factory"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(MAIN), str(source), str(MAIN / "zkt_factory_trial.c"),
                    str(MAIN / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)
