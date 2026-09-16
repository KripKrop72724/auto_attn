#include "delivery_scheduler.h"
#include <assert.h>
#include <stdio.h>
int main(void)
{
    delivery_scheduler_t state={0}; unsigned served[DS_LANES]={0}, last[DS_LANES]={0};
    for(unsigned step=0;step<100000;step++) {
        int lane=ds_pick(&state,63,step,false);assert(lane>=0);
        ds_attempted(&state,(unsigned)lane);ds_complete(&state,(unsigned)lane,step,true,0);
        served[lane]++;last[lane]=step;
        if(step>=20) for(unsigned i=0;i<DS_LANES;i++) assert(step-last[i]<20);
    }
    assert(served[0]+served[1]==80000);
    assert(served[2]+served[3]==10000);
    assert(served[4]+served[5]==10000);
    assert(ds_pick(&state,63,100000,true)>=2);
    ds_complete(&state,0,100000,false,0);
    for(unsigned step=100000;step<105000;step++) {
        int lane=ds_pick(&state,63,step,false);assert(lane!=0);
        ds_attempted(&state,(unsigned)lane);
    }
    assert(ds_pick(&state,1,105000,false)==0);
    ds_complete(&state,0,105000,false,0);assert(state.retry_at[0]==115000);
    ds_complete(&state,0,115000,true,0);assert(state.retry_at[0]==0);
    puts("delivery scheduler host regression tests passed");
}
