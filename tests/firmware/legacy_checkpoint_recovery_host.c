#include "legacy_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static lq_checkpoint_t saved;
static bool load_failed, commit_failed, commit_uncertain;
static unsigned commits;
static int load(void *context, lq_checkpoint_t *out)
{
    (void)context;
    if (load_failed) { errno=EIO; return -1; }
    *out=saved;
    return 1;
}
static bool commit(void *context, const lq_checkpoint_t *in)
{
    (void)context; ++commits;
    if (commit_failed) { errno=EIO; return false; }
    saved=*in;
    if (commit_uncertain) { errno=EIO; return false; }
    return true;
}
static const char original[]="first\nlast\n";
static void verify_file(void)
{
    FILE *file=fopen("retained","rb"); char actual[sizeof(original)];
    assert(file && fread(actual,1,sizeof(actual),file)==sizeof(original)-1);
    assert(feof(file) && !ferror(file) && fclose(file)==0);
    assert(!memcmp(actual,original,sizeof(original)-1));
}
static void seed(unsigned fault)
{
    FILE *file=fopen("retained","wb");
    assert(file && fwrite(original,1,sizeof(original)-1,file)==sizeof(original)-1 && fclose(file)==0);
    saved=(lq_checkpoint_t){.version=1,.generation=7,.offset=6,.prefix_crc=dq_crc32(original,6)};
    if (fault==1) saved.version=3;
    if (fault==2) saved.generation=0;
    if (fault==3) ++saved.prefix_crc;
    if (fault==4) { saved.offset=5; saved.prefix_crc=dq_crc32(original,5); }
    saved.crc=dq_crc32(&saved,offsetof(lq_checkpoint_t,crc));
    if (fault==0) ++saved.crc;
    load_failed=commit_failed=commit_uncertain=false; commits=0;
}
static void inspect(legacy_queue_t *queue, lq_token_t *token, lq_checkpoint_t *bytes)
{
    memset(queue,0,sizeof(*queue));
    assert(lq_open_step(queue,"retained",(lq_port_t){load,commit,NULL})==DQ_CORRUPT);
    assert(!queue->ready && queue->checkpoint_corrupt);
    assert(lq_checkpoint_evidence(queue,bytes,sizeof(*bytes),token)==DQ_OK);
    assert(!memcmp(bytes,&saved,sizeof(saved)) && token->checkpoint_evidence && token->evidence_required);
}
int main(void)
{
    legacy_queue_t queue; lq_token_t token; lq_checkpoint_t bytes;
    for (unsigned fault=0;fault<5;++fault) {
        seed(fault); inspect(&queue,&token,&bytes);
        assert(!commits && lq_settle(&queue,&token)==DQ_STALE);
        lq_token_t wrong=token; wrong.crc^=1;
        assert(lq_settle_evidence(&queue,&wrong)==DQ_STALE && !commits);
        wrong=token; wrong.evidence_required=false;
        assert(lq_settle_evidence(&queue,&wrong)==DQ_STALE && !commits);
        wrong=token; wrong.checkpoint_evidence=false;
        assert(lq_settle_evidence(&queue,&wrong)==DQ_IO && !commits);
        /* Lost ADD acknowledgement, then reset: identical custody identity and
         * bytes; the original checkpoint and every retained byte still exist. */
        lq_token_t prior=token; inspect(&queue,&token,&bytes);
        assert(token.generation==prior.generation && token.crc==prior.crc && !commits);
        load_failed=true; assert(lq_settle_evidence(&queue,&token)==DQ_IO && !commits);
        load_failed=false;
        lq_checkpoint_t original_checkpoint=saved;
        saved.prefix_crc^=1;
        assert(lq_settle_evidence(&queue,&token)==DQ_STALE && !commits);
        saved=original_checkpoint;
        commit_failed=true; assert(lq_settle_evidence(&queue,&token)==DQ_IO && commits==1);
        assert(!memcmp(&saved,&bytes,sizeof(saved))); verify_file();
        commit_failed=false;
        assert(lq_settle_evidence(&queue,&token)==DQ_OK && commits==2);
        assert(!saved.offset && !saved.prefix_crc && saved.generation==original_checkpoint.generation+1);
        assert(saved.version==1 && !queue.ready && !queue.checkpoint_corrupt);
        assert(lq_settle_evidence(&queue,&token)==DQ_STALE); verify_file();
        assert(lq_open_step(&queue,"retained",(lq_port_t){load,commit,NULL})==DQ_OK);
        char row[64]; assert(lq_peek(&queue,row,sizeof(row),&token)==DQ_OK);
        assert(!strcmp(row,"first\n") && !token.checkpoint_evidence && !token.evidence_required);
        assert(lq_settle(&queue,&token)==DQ_OK);
        assert(lq_peek(&queue,row,sizeof(row),&token)==DQ_OK && !strcmp(row,"last\n"));
        verify_file();
    }
    seed(0); inspect(&queue,&token,&bytes); commit_uncertain=true;
    assert(lq_settle_evidence(&queue,&token)==DQ_IO && !saved.offset);
    commit_uncertain=false; memset(&queue,0,sizeof(queue));
    assert(lq_open_step(&queue,"retained",(lq_port_t){load,commit,NULL})==DQ_OK);
    assert(lq_settle_evidence(&queue,&token)==DQ_STALE); verify_file();
    seed(0); saved.generation=UINT32_MAX; inspect(&queue,&token,&bytes);
    assert(lq_settle_evidence(&queue,&token)==DQ_CORRUPT && !commits); verify_file();
    puts("Corrupt legacy checkpoints: exact custody, lost replies, bounded reset and retained replay passed");
}
