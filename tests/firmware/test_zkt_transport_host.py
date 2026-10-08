"""Exercise production prepared-buffer reads with fragmented/coalesced packets."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_prepared_transport_rejects_wrong_session_truncation_and_oversized_frames(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    reads = source[source.index("static int64_t zk_io_deadline("):source.index("static bool send_all(")]
    stream = source[source.index("static bool zk_recv_data_stream("):source.index("static bool zk_send_command(")]
    command = source[source.index("static bool zk_send_command("):
                     source.index("static bool zk_send_ack_only(int sock, uint16_t session_id, int64_t deadline)\n{")]
    bounded_range = source[source.index("static bool zk_read_bounded_range("):
                           source.index("static bool zk_close_bounded_buffer(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "zkt_socket_io.h"
#include "zkt_record.h"
#define ZKT_IO_TIMEOUT_SEC 90
#define MACHINE_PREPARE_DATA_1 0x5050
#define MACHINE_PREPARE_DATA_2 0x7d82
#define CMD_DATA 1501
#define CMD_PREPARE_DATA 1500
#define CMD_READ_BUFFER_CHUNK 1504
#define CMD_ACK_OK 2000
#define CMD_REG_EVENT 500
#define CMD_CONNECT 1000
#define USHRT_MAX_ZK 65535
#define ZKT_BUFFER_CHUNK_BYTES 65472
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
typedef struct {uint16_t command,checksum,session_id,reply_id;} zk_header_t;
typedef struct {uint16_t marker_1,marker_2;uint32_t length;} zk_tcp_header_t;
typedef struct {uint16_t session_id,reply_id;} zk_context_t;
typedef struct {uint16_t code,session_id,reply_id;uint8_t *data;size_t data_len;} zk_response_t;
typedef struct {bool prepared;uint32_t size;uint8_t *direct_data;} zk_bounded_buffer_t;
static void write_le32(uint8_t *p,uint32_t value)
{for(unsigned i=0;i<4;++i)p[i]=(uint8_t)(value>>(8*i));}
static uint8_t input[2048];
static size_t total,position,fragment=1;
static unsigned acknowledgements;
static bool preserve_ok = true, preserved;
static unsigned captures;
static int64_t now, fragment_delay, preservation_delay;
static int64_t esp_timer_get_time(void) { return now; }
static bool zk_preserve_live_packet(const uint8_t *packet,size_t length)
{assert(length==9 && packet[8]=='x'); ++captures; preserved=preserve_ok; now+=preservation_delay; return preserve_ok;}
static int recv(int sock,void *out,size_t count,int flags)
{
    (void)sock;
    (void)flags;
    if (position == total) {
        return 0;
    }
    if (count > fragment) {
        count = fragment;
    }
    if (count > total - position) {
        count = total - position;
    }
    memcpy(out, input + position, count);
    position += count;
    return (int)count;
}
static bool zk_send_ack_only(int sock,uint16_t session,int64_t deadline)
{(void)sock;assert(session==12 && preserved);if(now>=deadline)return false;preserved=false;++acknowledgements;return true;}
bool zk_io_read_until(int sock,void *data,size_t count,int64_t deadline,zk_io_clock_t clock)
{
 size_t offset=0;
 while(offset<count){
  if(clock()>=deadline)return false;
  now+=fragment_delay;
  if(clock()>=deadline)return false;
  int got=recv(sock,(uint8_t*)data+offset,count-offset,0);
  if(got<=0)return false;
  offset+=(size_t)got;
 }
 return true;
}
bool zk_io_write_until(int sock,const void *data,size_t count,int64_t deadline,zk_io_clock_t clock)
{(void)sock;assert(data&&count);return clock()<deadline;}
static uint16_t zk_checksum(const uint8_t *data,size_t count)
{assert(data&&count);return 0;}
/* PRODUCTION */
static void frame(uint16_t command,uint16_t session,const char *body,size_t size)
{zk_tcp_header_t top={MACHINE_PREPARE_DATA_1,MACHINE_PREPARE_DATA_2,sizeof(zk_header_t)+(uint32_t)size};
 zk_header_t header={command,0,session,1};
 memcpy(input+total,&top,sizeof(top));total+=sizeof(top);
 memcpy(input+total,&header,sizeof(header));total+=sizeof(header);
 if(size){memcpy(input+total,body,size);total+=size;}}
static void synthetic_record(uint8_t raw[40],unsigned index)
{
 memset(raw,0,40);raw[0]=(uint8_t)index;raw[2]=(uint8_t)('0'+index);
 raw[26]=1;write_le32(raw+27,26U*12U*31U*86400U+index); /* January 1, 2026. */
 zkt_record_t decoded;assert(zkt_record_decode(raw,40,&decoded));
 assert(decoded.user_id[0]==(char)('0'+index));
}
int main(void){
 char out[16];size_t actual=0;
 for(fragment=1;fragment<=1024;fragment*=2){
  total=position=0;frame(CMD_DATA,12,"ab",2);frame(CMD_DATA,12,"cd",2);frame(CMD_ACK_OK,12,NULL,0);
  assert(zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));assert(actual==4&&!memcmp(out,"abcd",4));
 }
 total=position=0;frame(CMD_DATA,99,"abcd",4);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 total=position=0;frame(CMD_DATA,12,"abcde",5);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 total=position=0;frame(CMD_DATA,12,"abc",3);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 total=position=0;frame(CMD_DATA,12,"abcd",4);total--;
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 total=position=0;frame(CMD_DATA,12,"abcd",4);((zk_tcp_header_t*)input)->length=UINT32_MAX;
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 assert(!acknowledgements);
 total=position=0;frame(CMD_DATA,12,"abcd",4);((zk_tcp_header_t*)input)->length=UINT32_MAX;
 zk_context_t ctx={12,1};zk_response_t response;
 assert(!zk_send_command(1,&ctx,1,NULL,0,(uint8_t*)out,sizeof(out),&response));
 assert(position==sizeof(zk_tcp_header_t)); /* No drain of untrusted body. */
 assert(!zk_send_command(1,&ctx,1,(const uint8_t*)"x",SIZE_MAX,(uint8_t*)out,sizeof(out),&response));
 assert(!zk_send_command(1,&ctx,1,NULL,1,(uint8_t*)out,sizeof(out),&response));
 /* Synthetic mechanism, not a claim about any field incident: an otherwise
  * correct prefix must not certify a stale final record when more data follows. */
 uint8_t records[160],range_out[120];
 for(unsigned i=0;i<4;++i)synthetic_record(records+40*i,i+1);
 for(fragment=1;fragment<=1024;fragment*=2){
  total=position=0;frame(CMD_DATA,12,(char*)records,80);
  frame(CMD_DATA,12,(char*)records+120,40);frame(CMD_ACK_OK,12,NULL,0);
  assert(zk_recv_data_stream(1,12,range_out,120,&actual));
  assert(actual==120&&!memcmp(range_out,records,80)&&!memcmp(range_out+80,records+120,40));
  total=position=0;frame(CMD_DATA,12,(char*)records,80);
  frame(CMD_DATA,12,(char*)records+80,40); /* Stale, but individually valid. */
  frame(CMD_DATA,12,(char*)records+120,40);frame(CMD_ACK_OK,12,NULL,0);
  assert(!zk_recv_data_stream(1,12,range_out,120,&actual));
  total=position=0;frame(CMD_DATA,12,(char*)records,80);
  frame(CMD_DATA,99,(char*)records+80,40);frame(CMD_ACK_OK,12,NULL,0);
  assert(!zk_recv_data_stream(1,12,range_out,120,&actual));
  zk_bounded_buffer_t prepared={.prepared=true,.size=4000};
  total=position=0;frame(CMD_DATA,12,(char*)records,160);
  assert(!zk_read_bounded_range(1,&ctx,&prepared,284,range_out,120));
  total=position=0;frame(CMD_DATA,12,(char*)records,120);
  assert(zk_read_bounded_range(1,&ctx,&prepared,284,range_out,120));
  assert(!memcmp(range_out,records,120));
 }
 total=position=0;frame(CMD_REG_EVENT,12,"x",1);frame(CMD_DATA,12,"abcd",4);frame(CMD_ACK_OK,12,NULL,0);
 assert(zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 assert(captures==1 && acknowledgements==1);
 preserve_ok=false;
 total=position=0;frame(CMD_REG_EVENT,12,"x",1);frame(CMD_DATA,12,"abcd",4);
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual));
 assert(captures==2 && acknowledgements==1);
 total=position=0;frame(CMD_REG_EVENT,12,"x",1);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_send_command(1,&ctx,1,NULL,0,(uint8_t*)out,sizeof(out),&response));
 assert(captures==3 && acknowledgements==1);
 preserve_ok=true;
 total=position=0;frame(CMD_REG_EVENT,12,"x",1);frame(CMD_ACK_OK,12,NULL,0);
 assert(zk_send_command(1,&ctx,1,NULL,0,(uint8_t*)out,sizeof(out),&response));
 assert(captures==4 && acknowledgements==2 && response.code==CMD_ACK_OK);
 /* A byte trickle does not reset the full operation's 90-second budget. */
 now=0;fragment_delay=20000000;fragment=1;
 total=position=0;frame(CMD_DATA,12,"abcd",4);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_recv_data_stream(1,12,(uint8_t*)out,4,&actual) && position==4);
 now=0;total=position=0;frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_send_command(1,&ctx,1,NULL,0,(uint8_t*)out,sizeof(out),&response) && position==4);
 fragment_delay=0;fragment=1024;preservation_delay=60000000;
 now=0;total=position=0;frame(CMD_REG_EVENT,12,"x",1);frame(CMD_REG_EVENT,12,"x",1);frame(CMD_ACK_OK,12,NULL,0);
 assert(!zk_send_command(1,&ctx,1,NULL,0,(uint8_t*)out,sizeof(out),&response));
 assert(captures==6 && acknowledgements==3); /* Second frame preserved, not falsely ACKed. */
 /* A header-read retry must retain the same live-event preservation obligation.
  * The actual prepared-range command cannot ACK an unpreserved interleaved punch. */
 now=0;preservation_delay=0;fragment_delay=0;fragment=1024;
 zk_bounded_buffer_t header_source={.prepared=true,.size=44};uint8_t header_bytes[4];
 unsigned prior_captures=captures,prior_acks=acknowledgements;
 preserve_ok=false;total=position=0;
 frame(CMD_REG_EVENT,12,"x",1);frame(CMD_DATA,12,"\x28\0\0\0",4);
 assert(!zk_read_bounded_range(1,&ctx,&header_source,0,header_bytes,4));
 assert(captures==prior_captures+1 && acknowledgements==prior_acks);
 preserve_ok=true;total=position=0;
 frame(CMD_REG_EVENT,12,"x",1);frame(CMD_DATA,12,"\x28\0\0\0",4);
 assert(zk_read_bounded_range(1,&ctx,&header_source,0,header_bytes,4));
 assert(header_bytes[0]==40 && captures==prior_captures+2 && acknowledgements==prior_acks+1);
 return 0;
}
'''
    unit = tmp_path / "transport.c"
    unit.write_text(harness.replace("/* PRODUCTION */", reads + stream + command + bounded_range))
    executable = tmp_path / "transport"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(ROOT / "firmware/zone_lite/main"),
                    str(unit), str(ROOT / "firmware/zone_lite/main/zkt_record.c"),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
