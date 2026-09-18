#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include "durable_queue.h"

static unsigned calls, fail_at;
static bool directory_read_failure, directory_close_failure, checkpoint_read_failure;
static struct dirent *fault_readdir(DIR *dir)
{ if (directory_read_failure) { errno=EIO; return NULL; } return readdir(dir); }
static int fault_closedir(DIR *dir)
{ int result=closedir(dir); if(directory_close_failure) { errno=EIO; return -1; } return result; }
static bool fails(void) { return ++calls == fail_at; }
static FILE *fault_open(const char *p, const char *m)
{ if (fails()) { errno = EIO; return NULL; } return fopen(p,m); }
static size_t fault_write(const void *p, size_t s, size_t n, FILE *f)
{ if (fails()) { (void)fwrite(p,s,n/2,f); errno=EIO; return n/2; } return fwrite(p,s,n,f); }
static size_t fault_read(void *p, size_t s, size_t n, FILE *f)
{ if (fails()) { errno=EIO; return 0; } return fread(p,s,n,f); }
static int fault_flush(FILE *f)
{ if (fails()) { errno=EIO; return EOF; } return fflush(f); }
static int fault_sync(int fd)
{ if (fails()) { errno=EIO; return -1; } return fsync(fd); }
static int fault_close(FILE *f)
{ bool fail=fails(); int r=fclose(f); if(fail) { errno=EIO; return EOF; } return r; }
static int fault_seek(FILE *f, long offset, int origin)
{ if (fails()) { errno=EIO; return -1; } return fseek(f,offset,origin); }
static int fault_remove(const char *p)
{ if (fails()) { errno=EIO; return -1; } return remove(p); }
#define fopen fault_open
#define fwrite fault_write
#define fread fault_read
#define fflush fault_flush
#define fsync fault_sync
#define fclose fault_close
#define fseek fault_seek
#define remove fault_remove
#define readdir fault_readdir
#define closedir fault_closedir
#include "durable_queue.c"
#undef fopen
#undef fwrite
#undef fread
#undef fflush
#undef fsync
#undef fclose
#undef fseek
#undef remove
#undef readdir
#undef closedir

typedef struct { bool exists, uncertain; dq_checkpoint_t cp; } state_t;
static int load(void *ctx, dq_checkpoint_t *cp)
{ state_t *s=ctx; *cp=s->cp; return checkpoint_read_failure ? -1 : s->exists; }
static bool save(void *ctx, const dq_checkpoint_t *cp)
{
    state_t *s=ctx;
    bool fail=fails();
    if (!fail || s->uncertain) { s->cp=*cp; s->exists=true; }
    return !fail;
}
static void clean(void)
{
    DIR *d=opendir("."); assert(d);
    struct dirent *e;
    while ((e=readdir(d))) if (!strncmp(e->d_name,"fault-",6)) assert(!remove(e->d_name));
    closedir(d);
}
static void reset(durable_queue_t *q, state_t *s)
{
    fail_at=0; calls=0; clean(); memset(s,0,sizeof(*s));
    assert(dq_open(q,"fault-",(dq_port_t){load,save,NULL,s})==DQ_OK);
}
static void reopen(durable_queue_t *q, state_t *s)
{
    fail_at=0;
    assert(dq_open(q,"fault-",(dq_port_t){load,save,NULL,s})==DQ_OK);
}
int main(void)
{
    durable_queue_t q; state_t state;
    char row[8192]; size_t length; dq_token_t token;
    // An unavailable directory or checkpoint cannot initialize an empty queue.
    reset(&q,&state); clean(); memset(&state,0,sizeof(state));
    directory_read_failure=true;
    assert(dq_open(&q,"fault-",(dq_port_t){load,save,NULL,&state})==DQ_IO);
    assert(!state.exists && !q.ready);
    directory_read_failure=false; directory_close_failure=true;
    assert(dq_open(&q,"fault-",(dq_port_t){load,save,NULL,&state})==DQ_IO);
    assert(!state.exists && !q.ready);
    directory_close_failure=false; checkpoint_read_failure=true;
    assert(dq_open(&q,"fault-",(dq_port_t){load,save,NULL,&state})==DQ_IO);
    assert(!state.exists && !q.ready);
    checkpoint_read_failure=false;
    reset(&q,&state);
    assert(dq_append(&q,"valid record",12)==DQ_OK);
    dq_checkpoint_t before=state.cp;
    assert(dq_peek(&q,row,1,&length,&token)==DQ_BUFFER_SMALL && length==12);
    assert(memcmp(&before,&state.cp,sizeof(before))==0);
    assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK && length==12);
    assert(!memcmp(row,"valid record",12));
    /* Every injected call fails once, including short writes and uncertain
     * successful checkpoint commits reported as failures. Previous durable
     * records must remain readable, in order, after a restart. */
    for (unsigned uncertain=0; uncertain<2; ++uncertain) {
        for (unsigned target=1; target<=30; ++target) {
            reset(&q,&state); state.uncertain=uncertain;
            assert(dq_append(&q,"A",1)==DQ_OK);
            assert(dq_append(&q,"B",1)==DQ_OK);
            calls=0; fail_at=target;
            dq_result_t result=dq_append(&q,"C",1);
            reopen(&q,&state);
            for (char expected='A'; expected<='B'; ++expected) {
                assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK);
                assert(length==1 && row[0]==expected);
                assert(dq_settle(&q,&token)==DQ_OK);
            }
            dq_result_t peek=dq_peek(&q,row,sizeof(row),&length,&token);
            if (result==DQ_OK) assert(peek==DQ_OK);
            if (peek==DQ_OK) assert(length==1 && row[0]=='C');
            else assert(peek==DQ_EMPTY);
        }
        for (unsigned target=1; target<=30; ++target) {
            reset(&q,&state); state.uncertain=uncertain;
            assert(dq_append(&q,"A",1)==DQ_OK);
            assert(dq_append(&q,"B",1)==DQ_OK);
            assert(dq_append(&q,"C",1)==DQ_OK);
            assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK);
            calls=0; fail_at=target; (void)dq_settle(&q,&token);
            reopen(&q,&state);
            assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK);
            if(row[0]=='A') assert(dq_settle(&q,&token)==DQ_OK);
            for(char expected='B';expected<='C';++expected) {
                assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK);
                assert(length==1 && row[0]==expected);
                assert(dq_settle(&q,&token)==DQ_OK);
            }
            assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_EMPTY);
        }
    }
    /* Full segment transitions, failed unlink, and restart reclamation. */
    for(unsigned target=1; target<=40; ++target) {
        reset(&q,&state);
        memset(row,'x',sizeof(row));
        for(unsigned i=0;i<7;++i) assert(dq_append(&q,row,sizeof(row))==DQ_OK);
        calls=0; fail_at=target; (void)dq_append(&q,row,sizeof(row));
        reopen(&q,&state);
        unsigned delivered=0;
        while(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK) {
            assert(length==sizeof(row));
            assert(dq_settle(&q,&token)==DQ_OK); ++delivered;
        }
        assert(delivered>=7 && delivered<=8);
    }
    for(unsigned target=1;target<=30;++target) {
        reset(&q,&state); assert(dq_append(&q,"A",1)==DQ_OK);
        assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK);
        calls=0;fail_at=target;(void)dq_settle(&q,&token);
        reopen(&q,&state);
        if(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_OK)
            assert(dq_settle(&q,&token)==DQ_OK);
        assert(dq_peek(&q,row,sizeof(row),&length,&token)==DQ_EMPTY);
        DIR *d=opendir(".");assert(d);struct dirent *e;
        while((e=readdir(d))) assert(strncmp(e->d_name,"fault-",6));
        closedir(d);
    }
    clean();
    puts("durable queue injected I/O regression tests passed");
}
