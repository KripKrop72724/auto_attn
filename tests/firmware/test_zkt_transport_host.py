"""Exercise production prepared-buffer reads with fragmented/coalesced packets."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_prepared_transport_rejects_wrong_session_truncation_and_oversized_frames(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    reads = source[source.index("static bool recv_exact("):source.index("static bool send_all(")]
    stream = source[source.index("static bool zk_recv_data_stream("):source.index("static bool zk_send_command(")]
    command = source[source.index("static bool zk_send_command("):
                     source.index("static bool zk_send_ack_only(int sock, uint16_t session_id)\n{")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define MACHINE_PREPARE_DATA_1 0x5050
#define MACHINE_PREPARE_DATA_2 0x7d82
#define CMD_DATA 1501
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
static uint8_t input[2048];
static size_t total,position,fragment=1;
static unsigned acknowledgements;
static bool preserve_ok = true, preserved;
static unsigned captures;
static bool zk_preserve_live_packet(const uint8_t *packet,size_t length)
{assert(length==9 && packet[8]=='x'); ++captures; preserved=preserve_ok; return preserve_ok;}
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
static bool zk_send_ack_only(int sock,uint16_t session)
{(void)sock;assert(session==12 && preserved);preserved=false;++acknowledgements;return true;}
static bool send_all(int sock,const uint8_t *data,size_t count)
{(void)sock;assert(data&&count);return true;}
static uint16_t zk_checksum(const uint8_t *data,size_t count)
{assert(data&&count);return 0;}
/* PRODUCTION */
static void frame(uint16_t command,uint16_t session,const char *body,size_t size)
{zk_tcp_header_t top={MACHINE_PREPARE_DATA_1,MACHINE_PREPARE_DATA_2,sizeof(zk_header_t)+(uint32_t)size};
 zk_header_t header={command,0,session,1};
 memcpy(input+total,&top,sizeof(top));total+=sizeof(top);
 memcpy(input+total,&header,sizeof(header));total+=sizeof(header);
 if(size){memcpy(input+total,body,size);total+=size;}}
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
 return 0;
}
'''
    unit = tmp_path / "transport.c"
    unit.write_text(harness.replace("/* PRODUCTION */", reads + stream + command))
    executable = tmp_path / "transport"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
