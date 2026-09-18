#include "legacy_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
static unsigned read_bytes, reads, fail_read;
static bool fail_close;
static size_t checked_read(void *data,size_t size,size_t count,FILE *file)
{
    ++reads;
    if(reads==fail_read){errno=EIO;return 0;}
    size_t result=fread(data,size,count,file);read_bytes+=(unsigned)(result*size);return result;
}
static int checked_close(FILE *file)
{ int result=fclose(file);if(fail_close){errno=EIO;return -1;}return result; }
#define fread checked_read
#define fclose checked_close
#include "legacy_queue.c"
#undef fread
#undef fclose
static lq_checkpoint_t saved;
static unsigned commits;
static int load(void *context,lq_checkpoint_t *checkpoint)
{ (void)context;*checkpoint=saved;return 1; }
static bool save(void *context,const lq_checkpoint_t *checkpoint)
{ (void)context;(void)checkpoint;++commits;return true; }
int main(void)
{
    static char prefix[100000];memset(prefix,'x',sizeof(prefix));prefix[sizeof(prefix)-1]='\n';
    FILE *file=fopen("legacy","wb");assert(file);
    assert(fwrite(prefix,1,sizeof(prefix),file)==sizeof(prefix));assert(!fclose(file));
    saved=(lq_checkpoint_t){.version=1,.generation=7,.offset=sizeof(prefix),.prefix_crc=dq_crc32(prefix,sizeof(prefix))};
    saved.crc=dq_crc32(&saved,offsetof(lq_checkpoint_t,crc));
    lq_port_t port={load,save,NULL};
    for(unsigned failure=1;failure<=17;++failure){
        legacy_queue_t q={0};reads=read_bytes=0;fail_read=failure;fail_close=failure==17;
        assert(lq_open_step(&q,"legacy",port)==DQ_IO);
        assert(!q.ready && q.recovery_offset==0 && read_bytes<=LQ_RECOVERY_SLICE_BYTES);
        fail_read=0;fail_close=false;unsigned slices=0;
        dq_result_t result;
        do{
            read_bytes=0;result=lq_open_step(&q,"legacy",port);++slices;
            assert(read_bytes<=LQ_RECOVERY_SLICE_BYTES && !commits);
            if(result==DQ_PENDING)assert(!q.ready && q.recovering);
        }while(result==DQ_PENDING);
        assert(result==DQ_OK && q.ready && slices==13 && q.recovery_offset==sizeof(prefix));
        assert(q.checkpoint.offset==saved.offset && q.checkpoint.generation==7);
    }
    legacy_queue_t q={0};
    assert(lq_open_step(&q,"legacy",port)==DQ_PENDING);
    file=fopen("legacy","r+b");assert(file);assert(!fseek(file,90000,SEEK_SET));assert(fputc('y',file)!=EOF);assert(!fclose(file));
    dq_result_t result;
    do {result=lq_open_step(&q,"legacy",port);}while(result==DQ_PENDING);
    assert(result==DQ_CORRUPT && !q.ready && !commits);
    puts("bounded legacy recovery preserves checkpoints and retries failed slices");
}
