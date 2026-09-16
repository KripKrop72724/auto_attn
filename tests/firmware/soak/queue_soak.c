/* Host integration soak of the production queue, capacity policy and scheduler.
 * NVS and durable destinations are deterministic in-memory ports that survive
 * simulated worker restarts. This is not physical flash or whole-device evidence. */
#include "durable_queue.h"
#include "storage_budget.h"
#include "delivery_scheduler.h"
#include <assert.h>
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define TOTAL (8U*1024U*1024U)
#define ID_LIMIT 2000000U
static bool accepted[ID_LIMIT],confirmed[ID_LIMIT];
static storage_budget_t budget;
static uint64_t commits,failures,restarts,delivered,receipted,duplicates,peak;
typedef struct {dq_checkpoint_t checkpoint;bool present;unsigned lane;} context_t;
static context_t contexts[3];
static durable_queue_t queues[3];
static const char *names[]={"live","bulk","receipts"};
static size_t used(void) {
 DIR *directory=opendir(".");assert(directory);size_t bytes=0;struct dirent *entry;
 while((entry=readdir(directory))) {struct stat st;assert(!stat(entry->d_name,&st));if(S_ISREG(st.st_mode))bytes+=(size_t)st.st_size;}
 assert(!closedir(directory));if(bytes>peak)peak=bytes;return bytes;
}
static int load(void *arg,dq_checkpoint_t *checkpoint) {
 context_t *context=arg;if(!context->present)return 0;*checkpoint=context->checkpoint;return 1;
}
static bool commit(void *arg,const dq_checkpoint_t *checkpoint) {
 context_t *context=arg;commits++;
 if(commits%509==0){failures++;return false;}
 context->checkpoint=*checkpoint;context->present=true;
 if(commits%997==0){failures++;return false;}
 return true;
}
static bool admit(void *arg,size_t bytes) {
 context_t *context=arg;
 return storage_budget_admit(&budget,TOTAL,used(),bytes,context->lane==0?SB_LIVE:context->lane==1?SB_HISTORICAL:SB_RECOVERY);
}
static void reopen(unsigned index) {
 dq_port_t port={load,commit,admit,&contexts[index]};
 dq_result_t result=dq_open(&queues[index],names[index],port);
 assert(result==DQ_OK || result==DQ_IO);if(result==DQ_IO)reopen(index);
}
typedef struct {uint32_t id;unsigned char padding[252];} record_t;
static dq_result_t append(unsigned lane,uint32_t id) {
 assert(id<ID_LIMIT);record_t record={.id=id};memset(record.padding,(int)(id%251),sizeof(record.padding));
 dq_result_t result=dq_append(&queues[lane],&record,sizeof(record));
 assert(result==DQ_OK || result==DQ_IO || result==DQ_FULL);
 if(result==DQ_IO)reopen(lane);
 return result;
}
static uint64_t milliseconds(void) {
 struct timespec ts;assert(!clock_gettime(CLOCK_MONOTONIC,&ts));return (uint64_t)ts.tv_sec*1000+(unsigned)ts.tv_nsec/1000000;
}
static void drain(delivery_scheduler_t *scheduler,uint64_t now,bool offline) {
 for(unsigned i=0;i<32;i++) {
  unsigned mask=0;for(unsigned j=0;j<3;j++)if(queues[j].checkpoint.depth)mask|=1U<<(j*2);
  int selected=ds_pick(scheduler,mask,now,false);if(selected<0)return;
  unsigned lane=(unsigned)selected/2;ds_attempted(scheduler,(unsigned)selected);
  if(offline){ds_complete(scheduler,(unsigned)selected,now,false,(uint32_t)now);continue;}
  record_t record;size_t length;dq_token_t token;
  dq_result_t result=dq_peek(&queues[lane],&record,sizeof(record),&length,&token);
  if(result==DQ_IO){reopen(lane);continue;}assert(result==DQ_OK);
  assert(length==sizeof(record) && record.id<ID_LIMIT);
  for(unsigned j=0;j<sizeof(record.padding);j++)assert(record.padding[j]==record.id%251);
  bool settled=false;
  if(lane<2) {
   if(accepted[record.id])duplicates++;else{accepted[record.id]=true;delivered++;}
   if(append(2,record.id)==DQ_OK)settled=true;
  }else {assert(accepted[record.id]);if(!confirmed[record.id]){confirmed[record.id]=true;receipted++;}settled=true;}
  if(settled){result=dq_settle(&queues[lane],&token);assert(result==DQ_OK || result==DQ_IO);if(result==DQ_IO)reopen(lane);}
  ds_complete(scheduler,(unsigned)selected,now,settled,0);
 }
}
int main(int argc,char **argv) {
 unsigned seconds=argc>1?(unsigned)strtoul(argv[1],NULL,10):86400;
 unsigned backlog=argc>2?(unsigned)strtoul(argv[2],NULL,10):100000;
 assert(seconds>=2 && seconds<=86400 && backlog<=100000);
 for(unsigned i=0;i<3;i++){contexts[i].lane=i;reopen(i);}
 delivery_scheduler_t scheduler={0};uint32_t bulk=0,live=0;uint64_t start=milliseconds(),last_report=0,last_restart=0;bool load_checked=false;
 while(milliseconds()-start<(uint64_t)seconds*1000) {
  uint64_t elapsed=milliseconds()-start;
  uint32_t live_due=(uint32_t)(elapsed<1800000?elapsed/100:18000+(elapsed-1800000)/1000);
  uint32_t bulk_due=backlog+(uint32_t)(elapsed/1000);
  for(unsigned i=0;i<16 && live<live_due;i++){if(append(0,1000000U+live+1)!=DQ_OK)break;live++;}
  for(unsigned i=0;i<16 && bulk<bulk_due;i++){if(append(1,bulk+1)!=DQ_OK)break;bulk++;}
  if(elapsed>=1800000 && !load_checked){assert(live>=17990);load_checked=true;puts("30_MINUTE_10_EPS_CAPTURE_PASSED");fflush(stdout);}
  bool offline=elapsed%120000>=20000 && elapsed%120000<50000;
  drain(&scheduler,elapsed,offline);
  if(elapsed/17000>last_restart){last_restart=elapsed/17000;for(unsigned i=0;i<3;i++)reopen(i);restarts++;}
  assert(used()<=TOTAL*75U/100U);
  if(elapsed/60000>last_report){last_report=elapsed/60000;printf("elapsed_ms=%llu bulk=%u live=%u accepted=%llu confirmed=%llu bytes=%zu peak=%llu restarts=%llu faults=%llu\n",(unsigned long long)elapsed,bulk,live,(unsigned long long)delivered,(unsigned long long)receipted,used(),(unsigned long long)peak,(unsigned long long)restarts,(unsigned long long)failures);fflush(stdout);}
  struct timespec pause={0,100000000};nanosleep(&pause,NULL);
 }
 /* Close the observation with all locally admitted records settled. Source
  * records not admitted remain behind the simulated authoritative cursor. */
 uint64_t finish=milliseconds()+120000;memset(&scheduler,0,sizeof(scheduler));
 while(queues[0].checkpoint.depth || queues[1].checkpoint.depth || queues[2].checkpoint.depth){assert(milliseconds()<finish);drain(&scheduler,milliseconds(),false);}
 for(uint32_t i=1;i<=bulk;i++)assert(confirmed[i]);
 for(uint32_t i=1;i<=live;i++)assert(confirmed[1000000U+i]);
 /* An uncertain append may preserve the next source row while the source
  * checkpoint correctly stays behind. That extra row is allowed; every such
  * delivered row must still have its matching durable confirmation. */
 assert(delivered==receipted && delivered>=(uint64_t)bulk+live && delivered<=(uint64_t)bulk+live+2);
 for(uint32_t i=bulk+2;i<1000000U;i++)assert(!accepted[i]);
 for(uint32_t i=1000000U+live+2;i<ID_LIMIT;i++)assert(!accepted[i]);
 printf("PASS seconds=%u bulk=%u live=%u confirmed=%llu duplicates=%llu peak_bytes=%llu simulated_restarts=%llu checkpoint_faults=%llu\n",seconds,bulk,live,(unsigned long long)receipted,(unsigned long long)duplicates,(unsigned long long)peak,(unsigned long long)restarts,(unsigned long long)failures);
 return 0;
}
