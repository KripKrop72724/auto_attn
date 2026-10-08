#include "zkt_factory_trial.h"
#include "durable_queue.h"
#include <string.h>

static const zf_target_t targets[] = {
    {"a1ff7b24-4dcb-4dde-ad41-1a8401c7b006", "PGB1254700027",
     "e068ee75073e3198f7894f04a249169eef96feb996d9b7db7e5a8c7a04829e91",
     {0xe0,0x72,0xa1,0xd6,0xf3,0x28}, 4},
    {"2f5cedd8-e314-47b0-8074-bc4bb8a603cc", "PGB1261300034",
     "191b63c5f18485a9aa7f705f77679f932e79f326e1bf87e3c4bb237d249fb786",
     {0xac,0x27,0x6e,0xa3,0x0a,0x08}, 1},
    {"a886e2d9-204d-425c-bc8f-ded85fc89874", "PGB1254700036",
     "00dcc3514b997570fcdf7495f7b8a85302bcff6c2670120d13245f93c0424e8b",
     {0xac,0x27,0x6e,0xa5,0x4c,0xd8}, 2},
};
const zf_target_t *zf_target(unsigned i) { return i < sizeof(targets)/sizeof(targets[0]) ? &targets[i] : NULL; }
int zf_target_match(const char *connector, const uint8_t mac[6], const char *serial)
{
    if (!connector || !mac || !serial) return -1;
    for (unsigned i=0; i<sizeof(targets)/sizeof(targets[0]); ++i)
        if (!strcmp(connector, targets[i].connector_id) && !strcmp(serial, targets[i].terminal_serial) &&
            !memcmp(mac, targets[i].mac, 6)) return (int)i;
    return -1;
}
bool zf_digest_parse(const char *hex, uint8_t out[32])
{
    if (!hex || !out || strlen(hex) != 64) return false;
    for (unsigned i=0; i<32; ++i) {
        unsigned v=0;
        for (unsigned j=0; j<2; ++j) {
            char c=hex[2*i+j];
            if (c >= '0' && c <= '9') v=v*16+(unsigned)(c-'0');
            else if (c >= 'a' && c <= 'f') v=v*16+(unsigned)(c-'a'+10);
            else return false;
        }
        out[i]=(uint8_t)v;
    }
    return true;
}
static void put32(uint8_t *p, uint32_t v) { for (unsigned i=0;i<4;++i) p[i]=(uint8_t)(v>>(8*i)); }
static uint32_t get32(const uint8_t *p) { return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24; }
static bool nonzero(const uint8_t *p) { uint8_t v=0;for(unsigned i=0;i<32;++i)v|=p[i];return v!=0; }
bool zf_proof_encode(const zf_proof_t *p, uint8_t out[ZF_PROOF_BYTES])
{
    uint8_t expected[32];
    const zf_target_t *target=p ? zf_target(p->target) : NULL;
    if (!p || !out || !target || !memchr(p->deployment_id,0,sizeof(p->deployment_id)) ||
        !p->deployment_id[0] || strspn(p->deployment_id,"0123456789abcdef-") != strlen(p->deployment_id) ||
        (p->state != ZF_VERIFIED && p->state != ZF_REVOKED && p->state != ZF_FALLBACK_INTENT) ||
        p->signed_image_bytes < 8192 || p->signed_image_bytes > ZF_FACTORY_SIZE || p->signed_image_bytes%4096 ||
        !zf_digest_parse(target->application_sha256,expected) || memcmp(p->factory_digest,expected,32) ||
        !nonzero(p->reader_digest) || !nonzero(p->signed_digest) || !nonzero(p->layout_digest)) return false;
    memset(out,0,ZF_PROOF_BYTES);memcpy(out,"ZFACT001",8);put32(out+8,1);
    put32(out+12,p->state);put32(out+16,p->target);put32(out+20,p->signed_image_bytes);
    memcpy(out+24,p->deployment_id,strlen(p->deployment_id));
    memcpy(out+72,p->reader_digest,32);memcpy(out+104,p->factory_digest,32);
    memcpy(out+136,p->signed_digest,32);memcpy(out+168,p->layout_digest,32);
    put32(out+252,dq_crc32(out,252));return true;
}
bool zf_proof_decode(const uint8_t bytes[ZF_PROOF_BYTES], zf_proof_t *p)
{
    if (!bytes || !p) return false;
    memset(p,0,sizeof(*p));p->state=(zf_state_t)get32(bytes+12);p->target=get32(bytes+16);
    p->signed_image_bytes=get32(bytes+20);memcpy(p->deployment_id,bytes+24,48);
    memcpy(p->reader_digest,bytes+72,32);memcpy(p->factory_digest,bytes+104,32);
    memcpy(p->signed_digest,bytes+136,32);memcpy(p->layout_digest,bytes+168,32);
    uint8_t canonical[ZF_PROOF_BYTES];
    if (!zf_proof_encode(p,canonical) || memcmp(bytes,canonical,ZF_PROOF_BYTES)) { memset(p,0,sizeof(*p));return false; }
    return true;
}
static bool commit(zf_proof_port_t port,const uint8_t bytes[ZF_PROOF_BYTES])
{
    uint8_t verify[ZF_PROOF_BYTES];
    return port.write && port.read && port.write(port.context,bytes) &&
        port.read(port.context,verify)==1 && !memcmp(bytes,verify,ZF_PROOF_BYTES);
}
bool zf_proof_create(zf_proof_port_t port,const zf_proof_t *p)
{
    uint8_t before[ZF_PROOF_BYTES],bytes[ZF_PROOF_BYTES];
    if (!p || p->state != ZF_VERIFIED || !port.read || !zf_proof_encode(p,bytes)) return false;
    int read=port.read(port.context,before);
    /* A different earlier attempt needs an explicit audited renewal, not an
     * automatic replacement of durable evidence during startup. */
    if (read==1) return !memcmp(bytes,before,ZF_PROOF_BYTES);
    return read==0 && commit(port,bytes);
}
bool zf_proof_transition(zf_proof_port_t port,const zf_proof_t *expected,zf_state_t next,zf_proof_t *out)
{
    if (!expected || !out || !port.read || (next != ZF_REVOKED && next != ZF_FALLBACK_INTENT) ||
        (expected->state != ZF_VERIFIED && expected->state != next)) return false;
    uint8_t before[ZF_PROOF_BYTES],actual[ZF_PROOF_BYTES],after[ZF_PROOF_BYTES];
    if (!zf_proof_encode(expected,before) || port.read(port.context,actual)!=1 || memcmp(before,actual,sizeof(before))) return false;
    zf_proof_t changed=*expected;changed.state=next;
    if (!zf_proof_encode(&changed,after) || !commit(port,after)) return false;
    *out=changed;return true;
}
bool zf_fallback_metadata(uint32_t count,uint32_t slot,const uint32_t seq[2],const uint32_t state[2],const bool crc[2])
{
    if (count!=2 || slot>1 || !seq || !state || !crc) return false;
    /* IDF states: NEW0, PENDING1, VALID2, INVALID3, ABORTED4, UNDEFINEDffffffff.
     * Factory fallback is proved only with one pending current OTA entry and
     * no other valid selection. A true generic rollback check may mean ota_1. */
    bool valid[2];for(unsigned i=0;i<2;++i)valid[i]=seq[i]!=UINT32_MAX && crc[i] && state[i]!=3 && state[i]!=4;
    if (valid[0]==valid[1]) return false;
    unsigned active=valid[0]?0:1;
    return seq[active]>0 && state[active]==1 && ((seq[active]-1)%count)==slot;
}
