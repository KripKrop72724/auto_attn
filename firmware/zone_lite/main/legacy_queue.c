#include "legacy_queue.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

static uint32_t extend(uint32_t crc, const unsigned char *data, size_t length)
{
    crc = ~crc;
    while (length--) {
        crc ^= *data++;
        for (unsigned bit=0; bit<8; bit++) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
static bool persist(legacy_queue_t *q, lq_checkpoint_t next)
{
    if (next.generation == UINT32_MAX) return false;
    if (!next.version) next.version = 1;
    next.generation++;
    next.crc = dq_crc32(&next, offsetof(lq_checkpoint_t, crc));
    errno=0;
    if (!q->port.commit(q->port.context, &next)) { q->ready=false; if(!errno)errno=EIO; return false; }
    q->checkpoint=next;
    return true;
}
dq_result_t lq_open_step(legacy_queue_t *q, const char *path, lq_port_t port)
{
    if (!q || !path || strlen(path)>=sizeof(q->path) || !port.load || !port.commit) return DQ_IO;
    if (!q->recovering || strcmp(q->path,path) || q->port.context!=port.context ||
        q->port.load!=port.load || q->port.commit!=port.commit) {
        memset(q,0,sizeof(*q)); strcpy(q->path,path); q->port=port;
        errno=0;
        int loaded=port.load(port.context,&q->checkpoint);
        lq_checkpoint_t *cp=&q->checkpoint;
        if (loaded<0) { if(!errno)errno=EIO; return DQ_IO; }
        if (loaded && ((cp->version!=1 && cp->version!=2) || !cp->generation ||
            cp->crc!=dq_crc32(cp,offsetof(lq_checkpoint_t,crc)))) {
            q->checkpoint_corrupt=true; return DQ_CORRUPT;
        }
        q->recovering=true;
    }
    lq_checkpoint_t *cp=&q->checkpoint;
    if (cp->offset>q->recovery_offset) {
        FILE *file=fopen(path,"rb");
        if (!file) return DQ_IO;
        unsigned char bytes[512];
        uint32_t position=q->recovery_offset, crc=q->recovery_crc;
        uint32_t remaining=cp->offset-position;
        if (remaining>LQ_RECOVERY_SLICE_BYTES) remaining=LQ_RECOVERY_SLICE_BYTES;
        errno=0;
        bool ok=fseek(file,(long)position,SEEK_SET)==0, boundary=true;
        int error=ok?0:(errno?errno:EIO);
        while (ok && remaining) {
            size_t n=remaining<sizeof(bytes)?remaining:sizeof(bytes);
            if (fread(bytes,1,n,file)!=n) { ok=false; error=errno?errno:EIO; break; }
            crc=extend(crc,bytes,n); remaining-=(uint32_t)n; position+=(uint32_t)n;
            if (position==cp->offset && cp->version==1 && bytes[n-1]!='\n') boundary=false;
        }
        if (ferror(file)) { ok=false; if(!error)error=errno?errno:EIO; }
        if (fclose(file)!=0) { ok=false; if(!error)error=errno?errno:EIO; }
        // Failed I/O retries exactly this slice, including a failed close.
        if (!ok) { errno=error; return DQ_IO; }
        if (!boundary) { q->recovering=false; q->checkpoint_corrupt=true; return DQ_CORRUPT; }
        q->recovery_offset=position; q->recovery_crc=crc;
        if (position<cp->offset) return DQ_PENDING;
        if (crc!=cp->prefix_crc) { q->recovering=false; q->checkpoint_corrupt=true; return DQ_CORRUPT; }
    }
    q->recovering=false; q->ready=true;
    return DQ_OK;
}
/* Synchronous convenience for host tools. Firmware owners use the stepped API. */
dq_result_t lq_open(legacy_queue_t *q, const char *path, lq_port_t port)
{
    if (!q) return DQ_IO;
    memset(q,0,sizeof(*q));
    dq_result_t result;
    do { result=lq_open_step(q,path,port); } while(result==DQ_PENDING);
    return result;
}
dq_result_t lq_peek(legacy_queue_t *q, char *data, size_t capacity, lq_token_t *token)
{
    if (!q || !q->ready || !data || capacity<2 || capacity>DQ_MAX_RECORD_BYTES+1 || !token) return DQ_IO;
    FILE *file=fopen(q->path,"rb");
    if (!file) return errno==ENOENT && !q->checkpoint.offset ? DQ_EMPTY : DQ_IO;
    if (fseek(file,(long)q->checkpoint.offset,SEEK_SET)!=0) {
        int error=errno?errno:EIO; fclose(file); errno=error; return DQ_IO;
    }
    errno=0;
    size_t n=0; int ch=EOF;
    while (n<capacity-1 && (ch=fgetc(file))!=EOF) {
        data[n++]=(char)ch;
        if (ch=='\n') break;
    }
    bool ok=!ferror(file);
    int error=ok?0:(errno?errno:EIO);
    if (fclose(file)!=0) { ok=false; if(!error)error=errno?errno:EIO; }
    if (!ok) { errno=error; return DQ_IO; }
    if (!n && ch==EOF) return DQ_EMPTY;
    if (n>UINT32_MAX-q->checkpoint.offset) return DQ_CORRUPT;
    data[n]=0;
    *token=(lq_token_t){.generation=q->checkpoint.generation,.offset=q->checkpoint.offset,
        .end=q->checkpoint.offset+(uint32_t)n,.crc=dq_crc32(data,n),
        .evidence_required=q->checkpoint.version==2 || data[n-1]!='\n'};
    return DQ_OK;
}
dq_result_t lq_checkpoint_evidence(legacy_queue_t *q, void *data, size_t capacity, lq_token_t *token)
{
    if (!q || !data || !token || capacity<sizeof(q->checkpoint) ||
        q->ready || !q->checkpoint_corrupt) return DQ_IO;
    memcpy(data,&q->checkpoint,sizeof(q->checkpoint));
    *token=(lq_token_t){.generation=q->checkpoint.generation,.end=sizeof(q->checkpoint),
        .crc=dq_crc32(data,sizeof(q->checkpoint)),.evidence_required=true,.checkpoint_evidence=true};
    return DQ_OK;
}
static dq_result_t settle_checkpoint(legacy_queue_t *q, const lq_token_t *token)
{
    if (!q->checkpoint_corrupt || q->ready || !q->port.load || !q->port.commit ||
        !token->evidence_required || token->generation!=q->checkpoint.generation ||
        token->offset || token->end!=sizeof(q->checkpoint) ||
        token->crc!=dq_crc32(&q->checkpoint,sizeof(q->checkpoint))) return DQ_STALE;
    lq_checkpoint_t actual;
    errno=0;
    int loaded=q->port.load(q->port.context,&actual);
    if (loaded<0) { if(!errno)errno=EIO; return DQ_IO; }
    if (loaded!=1 || memcmp(&actual,&q->checkpoint,sizeof(actual))) return DQ_STALE;
    /* Never wrap a generation or pretend this proves removed bytes existed.
     * The original cursor is already in ADD custody; retained files stay
     * untouched and are read again from the beginning after this commit. */
    if (actual.generation==UINT32_MAX) return DQ_CORRUPT;
    lq_checkpoint_t next={.version=1,.generation=actual.generation};
    if (!persist(q,next)) return DQ_IO;
    q->ready=q->recovering=q->empty_cached=q->checkpoint_corrupt=false;
    q->recovery_offset=q->recovery_crc=0;
    return DQ_OK;
}
static dq_result_t settle(legacy_queue_t *q, const lq_token_t *token, bool custody)
{
    if (!q || !token) return DQ_IO;
    if (token->checkpoint_evidence) return custody ? settle_checkpoint(q,token) : DQ_STALE;
    if (!q->ready) return DQ_IO;
    if (!custody && (token->evidence_required || q->checkpoint.version==2)) return DQ_STALE;
    if (token->generation!=q->checkpoint.generation || token->offset!=q->checkpoint.offset ||
        token->end<=token->offset || token->end-token->offset>DQ_MAX_RECORD_BYTES) return DQ_STALE;
    FILE *file=fopen(q->path,"rb");
    if (!file) return DQ_IO;
    errno=0;
    bool ok=fseek(file,(long)token->offset,SEEK_SET)==0;
    int error=ok?0:(errno?errno:EIO);
    uint32_t remaining=token->end-token->offset, crc=0, prefix=q->checkpoint.prefix_crc;
    unsigned char bytes[512]; bool newline=false;
    while (ok && remaining) {
        size_t n=remaining<sizeof(bytes)?remaining:sizeof(bytes);
        if (fread(bytes,1,n,file)!=n) { ok=false; error=errno?errno:EIO; break; }
        crc=extend(crc,bytes,n); prefix=extend(prefix,bytes,n); remaining-=(uint32_t)n;
        if (!remaining) newline=bytes[n-1]=='\n';
    }
    if (ferror(file)) { ok=false; if(!error)error=errno?errno:EIO; }
    if (fclose(file)!=0) { ok=false; if(!error)error=errno?errno:EIO; }
    if (!ok) { errno=error; return DQ_IO; }
    if ((!custody && !newline) || crc!=token->crc) return DQ_STALE;
    lq_checkpoint_t next=q->checkpoint;
    next.offset=token->end; next.prefix_crc=prefix; next.version=newline?1:2;
    return persist(q,next)?DQ_OK:DQ_IO;
}
dq_result_t lq_settle(legacy_queue_t *q, const lq_token_t *token)
{ return settle(q, token, false); }
dq_result_t lq_settle_evidence(legacy_queue_t *q, const lq_token_t *token)
{ return settle(q, token, true); }

dq_result_t lq_reclaim(legacy_queue_t *q)
{
    if (!q || !q->ready) return DQ_IO;
    struct stat st;
    if (stat(q->path,&st)!=0) return errno==ENOENT && !q->checkpoint.offset ? DQ_EMPTY : DQ_IO;
    if ((uint64_t)st.st_size!=q->checkpoint.offset) return DQ_STALE;
    lq_checkpoint_t next=q->checkpoint;
    next.offset=0; next.prefix_crc=0; next.version=1;
    // Clear before unlink: interruption can only replay the old settled file.
    // A new append after unlink can never inherit an old nonzero checkpoint.
    if (!persist(q,next)) return DQ_IO;
    return remove(q->path)==0 ? DQ_OK : DQ_IO;
}
