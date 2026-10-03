"""Exercise production prepared-buffer reads with fragmented/coalesced packets."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_prepared_transport_rejects_wrong_session_truncation_and_oversized_frames(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    reads = source[source.index("static bool recv_exact("):source.index("static bool send_all(")]
    stream = source[source.index("static bool zk_recv_data_stream("):source.index("static bool zk_send_command(")]
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
#define ZKT_BUFFER_CHUNK_BYTES 65472
#define ESP_LOGW(...) ((void)0)
typedef struct {uint16_t command,checksum,session_id,reply_id;} zk_header_t;
typedef struct {uint16_t marker_1,marker_2;uint32_t length;} zk_tcp_header_t;
static uint8_t input[2048];
static size_t total,position,fragment=1;
static unsigned acknowledgements;
static int recv(int sock,void *out,size_t count,int flags)
{(void)sock;(void)flags;if(position==total)return 0;
 if(count>fragment)count=fragment;if(count>total-position)count=total-position;
 memcpy(out,input+position,count);position+=count;return (int)count;}
static bool zk_send_ack_only(int sock,uint16_t session)
{(void)sock;assert(session==12);++acknowledgements;return true;}
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
 return 0;
}
'''
    unit = tmp_path / "transport.c"
    unit.write_text(harness.replace("/* PRODUCTION */", reads + stream))
    executable = tmp_path / "transport"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
