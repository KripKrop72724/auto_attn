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
    next.version = 1;
    next.generation++;
    next.crc = dq_crc32(&next, offsetof(lq_checkpoint_t, crc));
    if (!q->port.commit(q->port.context, &next)) { q->ready=false; return false; }
    q->checkpoint=next;
    return true;
}
dq_result_t lq_open(legacy_queue_t *q, const char *path, lq_port_t port)
{
    if (!q || !path || strlen(path)>=sizeof(q->path) || !port.load || !port.commit) return DQ_IO;
    memset(q,0,sizeof(*q)); strcpy(q->path,path); q->port=port;
    int loaded=port.load(port.context,&q->checkpoint);
    lq_checkpoint_t *cp=&q->checkpoint;
    if (loaded<0 || (loaded && (cp->version!=1 || !cp->generation ||
        cp->crc!=dq_crc32(cp,offsetof(lq_checkpoint_t,crc))))) return DQ_CORRUPT;
    if (cp->offset) {
        FILE *file=fopen(path,"rb");
        if (!file) return DQ_IO;
        unsigned char bytes[512]; uint32_t remaining=cp->offset, crc=0;
        bool ok=true;
        while (remaining) {
            size_t n=remaining<sizeof(bytes)?remaining:sizeof(bytes);
            if (fread(bytes,1,n,file)!=n) { ok=false; break; }
            crc=extend(crc,bytes,n); remaining-=(uint32_t)n;
            if (!remaining && bytes[n-1]!='\n') ok=false;
        }
        if (ferror(file)) ok=false;
        if (fclose(file)!=0) ok=false;
        if (!ok || crc!=cp->prefix_crc) return DQ_CORRUPT;
    }
    q->ready=true;
    return DQ_OK;
}
dq_result_t lq_peek(legacy_queue_t *q, char *data, size_t capacity, lq_token_t *token)
{
    if (!q || !q->ready || !data || capacity<2 || !token) return DQ_IO;
    FILE *file=fopen(q->path,"rb");
    if (!file) return errno==ENOENT && !q->checkpoint.offset ? DQ_EMPTY : DQ_IO;
    if (fseek(file,(long)q->checkpoint.offset,SEEK_SET)!=0) { fclose(file); return DQ_IO; }
    size_t n=0; int ch=EOF;
    while (n<capacity-1 && (ch=fgetc(file))!=EOF) {
        data[n++]=(char)ch;
        if (ch=='\n') break;
    }
    bool ok=!ferror(file);
    if (fclose(file)!=0) ok=false;
    if (!ok) return DQ_IO;
    if (!n && ch==EOF) return DQ_EMPTY;
    if (data[n-1]!='\n' || n>UINT32_MAX-q->checkpoint.offset) return DQ_CORRUPT;
    data[n]=0;
    *token=(lq_token_t){q->checkpoint.generation,q->checkpoint.offset,
        q->checkpoint.offset+(uint32_t)n,dq_crc32(data,n)};
    return DQ_OK;
}
dq_result_t lq_settle(legacy_queue_t *q, const lq_token_t *token)
{
    if (!q || !q->ready || !token) return DQ_IO;
    if (token->generation!=q->checkpoint.generation || token->offset!=q->checkpoint.offset ||
        token->end<=token->offset || token->end-token->offset>DQ_MAX_RECORD_BYTES) return DQ_STALE;
    FILE *file=fopen(q->path,"rb");
    if (!file) return DQ_IO;
    bool ok=fseek(file,(long)token->offset,SEEK_SET)==0;
    uint32_t remaining=token->end-token->offset, crc=0, prefix=q->checkpoint.prefix_crc;
    unsigned char bytes[512];
    while (ok && remaining) {
        size_t n=remaining<sizeof(bytes)?remaining:sizeof(bytes);
        if (fread(bytes,1,n,file)!=n) { ok=false; break; }
        crc=extend(crc,bytes,n); prefix=extend(prefix,bytes,n); remaining-=(uint32_t)n;
        if (!remaining && bytes[n-1]!='\n') ok=false;
    }
    if (ferror(file)) ok=false;
    if (fclose(file)!=0) ok=false;
    if (!ok || crc!=token->crc) return DQ_STALE;
    lq_checkpoint_t next=q->checkpoint;
    next.offset=token->end; next.prefix_crc=prefix;
    return persist(q,next)?DQ_OK:DQ_IO;
}
dq_result_t lq_reclaim(legacy_queue_t *q)
{
    if (!q || !q->ready) return DQ_IO;
    struct stat st;
    if (stat(q->path,&st)!=0) return errno==ENOENT && !q->checkpoint.offset ? DQ_EMPTY : DQ_IO;
    if ((uint64_t)st.st_size!=q->checkpoint.offset) return DQ_STALE;
    lq_checkpoint_t next=q->checkpoint;
    next.offset=0; next.prefix_crc=0;
    // Clear before unlink: interruption can only replay the old settled file.
    // A new append after unlink can never inherit an old nonzero checkpoint.
    if (!persist(q,next)) return DQ_IO;
    return remove(q->path)==0 ? DQ_OK : DQ_IO;
}
