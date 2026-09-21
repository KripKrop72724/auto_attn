#include "durable_queue.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static dq_checkpoint_t durable;
static bool exists;
static unsigned writes;
static int load(void *ctx,dq_checkpoint_t *cp) { (void)ctx;*cp=durable;return exists; }
static bool commit(void *ctx,const dq_checkpoint_t *cp)
{ (void)ctx;durable=*cp;exists=true;++writes;return true; }
static void fix_crc(durable_queue_t *q) { q->checkpoint.crc=dq_crc32(&q->checkpoint,offsetof(dq_checkpoint_t,crc)); }
static dq_result_t audit_all(durable_queue_t *q,unsigned expected)
{
 dq_audit_t audit={0};unsigned calls=0;char buffer[DQ_MAX_RECORD_BYTES];dq_result_t result;
 unsigned before=writes;dq_checkpoint_t checkpoint=q->checkpoint;
 do {result=dq_audit_step(q,&audit,buffer,sizeof(buffer));++calls;assert(calls<=expected);}while(result==DQ_PENDING);
 assert(calls==expected && writes==before && !memcmp(&checkpoint,&q->checkpoint,sizeof(checkpoint)));
 return result;
}
int main(void)
{
 durable_queue_t q;dq_port_t port={load,commit,NULL,NULL};
 assert(dq_open(&q,"aud-",port)==DQ_OK);
 assert(audit_all(&q,1)==DQ_OK);
 char payload[1024];memset(payload,'a',sizeof(payload));
 for(unsigned i=0;i<130;++i)assert(dq_append(&q,payload,sizeof(payload))==DQ_OK);
 assert(audit_all(&q,130)==DQ_OK); // crosses two segment seals
 dq_checkpoint_t checkpoint=q.checkpoint;
 dq_audit_t audit={0};char buffer[1024];
 assert(dq_audit_step(&q,&audit,buffer,1)==DQ_BUFFER_SMALL);
 assert(audit.remaining==130 && audit.offset==0);
 assert(dq_audit_step(&q,&audit,buffer,sizeof(buffer))==DQ_PENDING);
 assert(audit.remaining==129);
 assert(dq_append(&q,"extra",5)==DQ_OK);
 assert(dq_audit_step(&q,&audit,buffer,sizeof(buffer))==DQ_PENDING);
 assert(audit.remaining==130); // changed generation restarts conservatively
 assert(audit_all(&q,131)==DQ_OK);
 q.checkpoint.depth++;fix_crc(&q);
 assert(audit_all(&q,1)==DQ_CORRUPT); // valid checksum, false count/sequence
 q.checkpoint=checkpoint;
 q.checkpoint.depth=0;fix_crc(&q);
 assert(audit_all(&q,1)==DQ_CORRUPT); // count zero cannot substitute for boundary
 q.checkpoint=checkpoint;
 FILE *file=fopen("aud-00000001.q","r+b");assert(file);
 assert(!fseek(file,16,SEEK_SET));assert(fputc('x',file)!=EOF);assert(!fclose(file));
 memset(&audit,0,sizeof(audit));unsigned before=writes;dq_result_t result;unsigned count=0;
 do {result=dq_audit_step(&q,&audit,buffer,sizeof(buffer));++count;}while(result==DQ_PENDING);
 assert(result==DQ_CORRUPT && count>1 && !audit.complete && writes==before);
 assert(!memcmp(&checkpoint,&q.checkpoint,sizeof(checkpoint)));
 assert(!remove("aud-00000001.q"));
 memset(&audit,0,sizeof(audit));
 do {result=dq_audit_step(&q,&audit,buffer,sizeof(buffer));}while(result==DQ_PENDING);
 assert(result==DQ_IO && !audit.complete && writes==before);
 puts("bounded queue audit rejects corrupt data, false counts and missing segments without settlement");
}
