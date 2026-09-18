#include "worker_retry.h"
#include <assert.h>
#include <stdio.h>
int main(void)
{
    worker_retry_t s={0};
    assert(worker_retry_allow(&s,0));assert(worker_retry_allow(&s,1000));assert(worker_retry_allow(&s,2000));
    for(uint32_t t=2001;t<600000;t+=37)assert(!worker_retry_allow(&s,t));
    assert(worker_retry_allow(&s,600000));assert(!worker_retry_allow(&s,600999));
    assert(worker_retry_allow(&s,601000));assert(!worker_retry_allow(&s,601999));
    assert(worker_retry_allow(&s,602000));assert(s.total==6);
    worker_retry_t wrap={0};
    assert(worker_retry_allow(&wrap,UINT32_MAX-100));assert(worker_retry_allow(&wrap,UINT32_MAX-50));
    assert(worker_retry_allow(&wrap,0));assert(!worker_retry_allow(&wrap,599898));
    assert(worker_retry_allow(&wrap,599899));
    assert(!worker_retry_allow(NULL,0));
    puts("worker retry regression tests passed");
}
