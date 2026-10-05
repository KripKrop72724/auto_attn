#include "zkt_legacy_inventory.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static zq_request_t request(unsigned domain, unsigned lane, unsigned operation)
{
    return (zq_request_t){.domain=domain,.lane=lane,.operation=operation,.deadline_us=100,
        .total=operation==ZQ_APPEND_BEGIN?1:0};
}
static void observe(zq_inventory_t *s, zq_request_t r, zq_reply_t out)
{
    zq_inventory_admitted(s,&r); zq_inventory_begin(s,&r); zq_inventory_complete(s,&r,&out);
}
static void empty_domains(zq_inventory_t *s, int omit)
{
    unsigned index=0;
    for(unsigned domain=0;domain<4;domain++) {
        /* Quarantine must be inspected after all ordinary queue reads. */
        unsigned actual=domain==2?ZQ_ATTENDANCE_LEGACY:domain==3?ZQ_QUARANTINE:domain;
        unsigned lanes=actual==ZQ_SEGMENTED?QS_HIK_SOURCE:zq_domain_lanes(actual);
        for(unsigned lane=0;lane<lanes;lane++,index++) {
            if((int)index==omit)continue;
            observe(s,request(actual,lane,ZQ_PEEK_BEGIN),(zq_reply_t){.result=DQ_EMPTY});
        }
    }
}
int main(void)
{
    zq_inventory_t s={0};
    assert(!zq_inventory_empty(&s));
    zq_inventory_invalidate(&s); empty_domains(&s,-1);
    assert(zq_inventory_empty(&s) && s.empty_mask==ZQ_INVENTORY_REQUIRED);
    for(unsigned missing=0;missing<ZQ_INVENTORY_LANES;missing++) {
        zq_inventory_invalidate(&s);empty_domains(&s,(int)missing);
        assert(!zq_inventory_empty(&s));
    }
    for(unsigned fault=DQ_OK;fault<=DQ_PENDING;fault++) {
        zq_inventory_invalidate(&s);empty_domains(&s,-1);
        observe(&s,request(ZQ_ADD_LEGACY,0,ZQ_PEEK_BEGIN),(zq_reply_t){.result=(dq_result_t)fault});
        assert(zq_inventory_empty(&s)==(fault==DQ_EMPTY));
    }
    zq_inventory_invalidate(&s);empty_domains(&s,-1);
    zq_request_t append=request(ZQ_ATTENDANCE_LEGACY,1,ZQ_APPEND_BEGIN);
    zq_inventory_admitted(&s,&append);
    assert(!zq_inventory_empty(&s) && s.empty_mask==0); /* Before execution. */
    /* A zero from an unverified snapshot cannot complete any domain. */
    for(unsigned verified=0;verified<2;verified++) {
        zq_inventory_invalidate(&s);empty_domains(&s,-1);
        observe(&s,request(ZQ_SEGMENTED,0,ZQ_SNAPSHOT),
            (zq_reply_t){.result=DQ_OK,.verified=verified,.depth=0});
        assert(zq_inventory_empty(&s)==(bool)verified);
    }
    zq_inventory_invalidate(&s);empty_domains(&s,-1);
    observe(&s,request(ZQ_SEGMENTED,0,ZQ_SNAPSHOT),(zq_reply_t){.result=DQ_OK,.verified=true,.depth=1});
    assert(!zq_inventory_empty(&s));
    /* First/unknown reads invalidate earlier quarantine absence evidence. */
    zq_inventory_invalidate(&s);
    for(unsigned lane=0;lane<3;lane++)observe(&s,request(ZQ_QUARANTINE,lane,ZQ_PEEK_BEGIN),(zq_reply_t){.result=DQ_EMPTY});
    assert(s.empty_mask==ZQ_INVENTORY_QUARANTINE);
    observe(&s,request(ZQ_ADD_LEGACY,0,ZQ_PEEK_BEGIN),(zq_reply_t){.result=DQ_EMPTY});
    assert(!(s.empty_mask&ZQ_INVENTORY_QUARANTINE));
    empty_domains(&s,-1);assert(zq_inventory_empty(&s));
    /* An already verified empty read uses the existing absence proof. */
    observe(&s,request(ZQ_ADD_LEGACY,0,ZQ_PEEK_BEGIN),(zq_reply_t){.result=DQ_EMPTY});
    assert(zq_inventory_empty(&s));
    observe(&s,request(ZQ_SEGMENTED,QS_HIK_SOURCE,ZQ_PEEK_BEGIN),(zq_reply_t){.result=DQ_EMPTY});
    assert(!zq_inventory_empty(&s));
    s.generation=UINT64_MAX;zq_inventory_invalidate(&s);empty_domains(&s,-1);
    assert(s.exhausted && !zq_inventory_empty(&s));
    puts("legacy inventory: all domains, corruption, replay and wrap checks passed");
    return 0;
}
