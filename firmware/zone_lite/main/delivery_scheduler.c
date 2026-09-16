#include "delivery_scheduler.h"

static int pair(unsigned mask, unsigned base, unsigned next)
{
    unsigned first=base+(next&1U), second=base+((next+1)&1U);
    if(mask&(1U<<first)) return (int)first;
    return mask&(1U<<second) ? (int)second : -1;
}
int ds_pick(const delivery_scheduler_t *s,unsigned mask,uint64_t now,bool background_due)
{
    if(!s) return -1;
    for(unsigned i=0;i<DS_LANES;i++) if(s->retry_at[i]>now) mask&=~(1U<<i);
    int live=pair(mask,0,s->live_next), bulk=pair(mask,2,s->bulk_next), proof=pair(mask,4,s->proof_next);
    int background=s->background_next&1U ? proof:bulk;
    if(background<0) background=s->background_next&1U ? bulk:proof;
    if(live>=0 && s->live_attempts<4 && !background_due) return live;
    return background>=0 ? background:live;
}
void ds_attempted(delivery_scheduler_t *s,unsigned lane)
{
    if(!s || lane>=DS_LANES) return;
    if(lane<2) { if(s->live_attempts<4)s->live_attempts++;s->live_next=lane+1; }
    else {
        s->live_attempts=0;
        if(lane<4) { s->bulk_next=lane-1;s->background_next=1; }
        else { s->proof_next=lane-3;s->background_next=0; }
    }
}
void ds_complete(delivery_scheduler_t *s,unsigned lane,uint64_t now,bool ok,uint32_t jitter)
{
    if(!s || lane>=DS_LANES) return;
    if(ok) {s->retry_at[lane]=0;s->backoff_ms[lane]=0;return;}
    uint32_t delay=s->backoff_ms[lane] ? s->backoff_ms[lane] : 5000;
    s->retry_at[lane]=now+delay+jitter%1000;
    s->backoff_ms[lane]=delay>=30000?60000:delay*2;
}
