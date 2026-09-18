#include "durable_queue.h"
#include "runtime_checkpoint.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { dq_checkpoint_t cp; bool exists, fail, full; } port_state_t;
static int load(void *arg, dq_checkpoint_t *cp)
{ port_state_t *s=arg; *cp=s->cp; return s->exists ? 1 : 0; }
static bool commit(void *arg, const dq_checkpoint_t *cp)
{ port_state_t *s=arg; if(s->fail) return false; s->cp=*cp; s->exists=true; return true; }
static bool admit(void *arg, size_t n)
{ (void)n; return !((port_state_t *)arg)->full; }
int main(int argc, char **argv)
{
    runtime_checkpoint_t runtime;
    memset(&runtime, 0, sizeof(runtime));
    runtime.version = RUNTIME_CHECKPOINT_VERSION;
    runtime.generation = 1;
    memset(runtime.source_chain, '0', 64);
    strcpy(runtime.truth_version, "test");
    runtime.crc = dq_crc32(&runtime, offsetof(runtime_checkpoint_t, crc));
    assert(runtime_checkpoint_valid(&runtime));
    for (size_t byte=0; byte<offsetof(runtime_checkpoint_t, crc)+sizeof(runtime.crc); byte++) {
        for (unsigned bit=0; bit<8; bit++) {
            ((unsigned char *)&runtime)[byte] ^= (unsigned char)(1U << bit);
            assert(!runtime_checkpoint_valid(&runtime));
            ((unsigned char *)&runtime)[byte] ^= (unsigned char)(1U << bit);
        }
    }
    assert(argc == 2);
    port_state_t state={0};
    dq_port_t port={load,commit,admit,&state};
    durable_queue_t q; assert(dq_open(&q,argv[1],port)==DQ_OK);
    size_t length; char data[8192]; dq_token_t token;
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_EMPTY);
    assert(dq_append(&q,"A",1)==DQ_OK); assert(dq_append(&q,"B",1)==DQ_OK);
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_OK && data[0]=='A');
    dq_token_t stale=token;
    assert(dq_settle(&q,&token)==DQ_OK);
    assert(dq_settle(&q,&stale)==DQ_STALE);
    assert(dq_open(&q,argv[1],port)==DQ_OK);
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_OK && data[0]=='B');
    state.fail=true;
    assert(dq_settle(&q,&token)==DQ_IO);
    state.fail=false;
    assert(dq_open(&q,argv[1],port)==DQ_OK);
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_OK && data[0]=='B');
    assert(dq_settle(&q,&token)==DQ_OK);
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_EMPTY);
    state.full=true; assert(dq_append(&q,"C",1)==DQ_FULL); state.full=false;
    memset(data,'x',sizeof(data));
    for(unsigned i=0;i<100;i++) { memcpy(data,&i,sizeof(i)); assert(dq_append(&q,data,sizeof(data))==DQ_OK); }
    for(unsigned i=0;i<100;i++) {
        assert(dq_open(&q,argv[1],port)==DQ_OK);
        assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_OK && length==sizeof(data));
        unsigned actual; memcpy(&actual,data,sizeof(actual)); assert(actual==i);
        assert(dq_settle(&q,&token)==DQ_OK);
    }
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_EMPTY);
    state.fail=true; assert(dq_append(&q,"uncommitted",11)==DQ_IO); state.fail=false;
    assert(dq_open(&q,argv[1],port)==DQ_OK);
    assert(dq_append(&q,"D",1)==DQ_OK);
    assert(dq_peek(&q,data,sizeof(data),&length,&token)==DQ_OK && length==1 && data[0]=='D');
    assert(dq_settle(&q,&token)==DQ_OK);
    state.cp.crc^=1;
    assert(dq_open(&q,argv[1],port)==DQ_CORRUPT);
    puts("durable queue host regression tests passed");
}
