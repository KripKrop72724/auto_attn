#include "storage_budget.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
int main(void)
{
    storage_budget_t budget={0};
    const size_t total=16*1024*1024;
    assert(storage_budget_admit(&budget,total,total/2,1024,SB_HISTORICAL));
    assert(!storage_budget_admit(&budget,total,(total*60+99)/100,1024,SB_HISTORICAL));
    assert(budget.bulk_paused);
    assert(!storage_budget_admit(&budget,total,total*56/100,1024,SB_HISTORICAL));
    assert(storage_budget_admit(&budget,total,total*54/100,1024,SB_HISTORICAL));
    assert(!budget.bulk_paused);
    size_t pressure=(total*70+99)/100;
    assert(!storage_budget_admit(&budget,total,pressure,1024,SB_HISTORICAL));
    assert(storage_budget_admit(&budget,total,pressure,1024,SB_LIVE));
    assert(storage_budget_admit(&budget,total,pressure,1024,SB_RECOVERY));
    size_t ceiling=total*75/100;
    assert(!storage_budget_admit(&budget,total,ceiling-4096,1,SB_LIVE));
    assert(storage_budget_admit(&budget,total,ceiling-4096,1,SB_RECOVERY));
    assert(!storage_budget_admit(&budget,total,ceiling-4095,1,SB_RECOVERY));
    assert(!storage_budget_admit(&budget,total,ceiling,1,SB_RECOVERY));
    assert(!storage_budget_admit(&budget,total,total-4096,1,SB_RECOVERY));
    assert(!storage_budget_admit(&budget,0,0,1,SB_LIVE));
    assert(!storage_budget_admit(&budget,total,total+1,1,SB_LIVE));
    assert(!storage_budget_admit(&budget,total,0,SIZE_MAX,SB_RECOVERY));
    assert(!storage_budget_admit(&budget,512*1024,0,1,SB_HISTORICAL));
    puts("storage budget regression tests passed");
}
