#include "legacy_queue.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
typedef struct { lq_checkpoint_t cp; bool exists, fail; } state_t;
static int load(void *arg,lq_checkpoint_t *cp)
{ state_t *s=arg; *cp=s->cp; return s->exists?1:0; }
static bool commit(void *arg,const lq_checkpoint_t *cp)
{ state_t *s=arg; if(s->fail)return false; s->cp=*cp;s->exists=true;return true; }
static void append(const char *path,const char *data)
{ FILE *f=fopen(path,"ab"); assert(f);assert(fputs(data,f)>=0);assert(fflush(f)==0);assert(fsync(fileno(f))==0);assert(fclose(f)==0); }
int main(int argc,char **argv)
{
    assert(argc==2); state_t state={0}; legacy_queue_t q;
    lq_port_t port={load,commit,&state}; char data[128]; lq_token_t token;
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_EMPTY);
    append(argv[1],"A\nB\nC\n");
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && !strcmp(data,"A\n"));
    append(argv[1],"D\n"); // Live append during a network request.
    assert(lq_settle(&q,&token)==DQ_OK);
    assert(lq_settle(&q,&token)==DQ_STALE);
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && !strcmp(data,"B\n"));
    state.fail=true; assert(lq_settle(&q,&token)==DQ_IO);state.fail=false;
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    for(char c='B';c<='D';c++) {
        assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && data[0]==c);
        assert(lq_settle(&q,&token)==DQ_OK);
    }
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_EMPTY);
    state.fail=true;assert(lq_reclaim(&q)==DQ_IO);state.fail=false;
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_reclaim(&q)==DQ_OK);
    append(argv[1],"new\n");
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && !strcmp(data,"new\n"));
    assert(lq_settle(&q,&token)==DQ_OK);
    // Replacing a file with different already-consumed bytes must not skip it.
    FILE *f=fopen(argv[1],"wb");assert(f);assert(fputs("bad\n",f)>=0);assert(fclose(f)==0);
    assert(lq_open(&q,argv[1],port)==DQ_CORRUPT);
    state=(state_t){0};
    assert(remove(argv[1])==0);
    append(argv[1],"partial");
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && token.evidence_required);
    assert(lq_settle(&q,&token)==DQ_STALE);
    append(argv[1],"\n");
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK);
    assert(lq_settle(&q,&token)==DQ_OK);assert(lq_reclaim(&q)==DQ_OK);
    // More than the old 64 KiB recovery cutoff, read without a second copy.
    for(unsigned i=0;i<10000;i++) append(argv[1],"legacy-record\n");
    for(unsigned i=0;i<10000;i++) {
        assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK);
        assert(lq_settle(&q,&token)==DQ_OK);
    }
    assert(lq_reclaim(&q)==DQ_OK);
    // An oversized row is raw custody only, including a valid-looking suffix.
    f=fopen(argv[1],"ab");assert(f);
    for(unsigned i=0;i<70000;i++) assert(fputc('x',f)!=EOF);
    assert(fflush(f)==0 && fsync(fileno(f))==0);assert(fclose(f)==0);
    append(argv[1],"{\"attendance\":true}\nnext\n");
    size_t preserved=0;
    do {
        assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK);
        assert(token.evidence_required);
        assert(lq_settle(&q,&token)==DQ_STALE);
        lq_token_t original=token;
        state.fail=true;assert(lq_settle_evidence(&q,&token)==DQ_IO);state.fail=false;
        assert(lq_open(&q,argv[1],port)==DQ_OK);
        assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK);
        assert(token.offset==original.offset && token.crc==original.crc);
        preserved+=token.end-token.offset;
        assert(lq_settle_evidence(&q,&token)==DQ_OK);
        assert(lq_open(&q,argv[1],port)==DQ_OK);
    } while(q.checkpoint.version==2);
    assert(preserved==70000+strlen("{\"attendance\":true}\n"));
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && !token.evidence_required && !strcmp(data,"next\n"));
    assert(lq_settle(&q,&token)==DQ_OK);assert(lq_reclaim(&q)==DQ_OK);
    append(argv[1],"unfinished");
    assert(lq_peek(&q,data,sizeof(data),&token)==DQ_OK && token.evidence_required);
    assert(lq_settle(&q,&token)==DQ_STALE);
    assert(lq_settle_evidence(&q,&token)==DQ_OK);
    assert(lq_open(&q,argv[1],port)==DQ_OK);
    assert(lq_reclaim(&q)==DQ_OK);
    puts("legacy queue host regression tests passed");
}
