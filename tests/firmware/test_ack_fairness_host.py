"""Actual ACK orchestration must yield to an eligible background sender."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_direct_delivery_cannot_reacquire_ahead_of_background(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "add_connector.c").read_text()
    start = source.index("static bool send_payload_and_wait_for_ack(")
    end = source.index("bool add_connector_send_payload(", start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <string.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef unsigned TickType_t;
typedef struct {bool valid;} add_reconcile_chunk_ack_t;
typedef struct {bool valid;} add_attendance_settlement_ack_t;
static add_reconcile_chunk_ack_t s_reconcile_chunk_ack;
static add_attendance_settlement_ack_t s_attendance_settlement_ack={true};
static void *s_outbox_task_handle=(void *)1,*current=(void *)2;
static int ack_lock,ack_sem,state_lock;
static int *s_ack_wait_lock=&ack_lock,*s_ack_sem=&ack_sem,*s_lock=&state_lock;
static atomic_bool s_background_ack_waiting;
static atomic_uint_least32_t s_background_ack_since_ms;
static bool s_ack_matched,fail_lock,fail_send,fail_ack,interleave;
static char s_waiting_ack[80];
static uint32_t now=100;
static unsigned sends;
static void *xTaskGetCurrentTaskHandle(void){return current;}
static int64_t monotonic_ms(void){return now;}
static bool send_payload_and_wait_for_ack(const char *,const char *,TickType_t,TickType_t,add_reconcile_chunk_ack_t *,add_attendance_settlement_ack_t *);
static int xSemaphoreTake(int *lock,unsigned timeout)
{
    (void)timeout;
    if(lock==s_ack_wait_lock){
        if(fail_lock)return 0;
        if(interleave){
            interleave=false;current=(void *)2;
            unsigned previous=sends;
            for(unsigned i=0;i<100000;++i)assert(!send_payload_and_wait_for_ack("direct","{}",10,10,NULL,NULL));
            assert(sends==previous);current=s_outbox_task_handle;
        }
        assert(!ack_lock);ack_lock=1;
    }
    if(lock==s_ack_sem && fail_ack)return 0;
    return 1;
}
static void xSemaphoreGive(int *lock){if(lock==s_ack_wait_lock){assert(ack_lock);ack_lock=0;}}
static bool send_payload(const char *type,const char *payload,bool ack,char *id)
{assert(ack_lock && type && payload && ack);++sends;strcpy(id,"message");s_ack_matched=true;return !fail_send;}
/* PRODUCTION */
int main(void)
{
    assert(send_payload_and_wait_for_ack("direct","{}",10,10,NULL,NULL));
    current=s_outbox_task_handle;interleave=true;
    assert(send_payload_and_wait_for_ack("background","{}",10,10,NULL,NULL));
    assert(!atomic_load(&s_background_ack_waiting));
    fail_lock=true;assert(!send_payload_and_wait_for_ack("background","{}",10,10,NULL,NULL));fail_lock=false;
    assert(!atomic_load(&s_background_ack_waiting));
    fail_send=true;assert(!send_payload_and_wait_for_ack("background","{}",10,10,NULL,NULL));fail_send=false;
    fail_ack=true;assert(!send_payload_and_wait_for_ack("background","{}",10,10,NULL,NULL));fail_ack=false;
    assert(!atomic_load(&s_background_ack_waiting) && !ack_lock);
    // A declared dead worker cannot block direct preservation indefinitely.
    current=(void *)2;atomic_store(&s_background_ack_waiting,true);atomic_store(&s_background_ack_since_ms,100);
    now=90099;assert(!send_payload_and_wait_for_ack("direct","{}",10,10,NULL,NULL));
    now=90100;assert(send_payload_and_wait_for_ack("direct","{}",10,10,NULL,NULL));
    // Unsigned elapsed arithmetic also handles the monotonic counter wrapping.
    atomic_store(&s_background_ack_since_ms,UINT32_MAX-50);now=100;
    assert(!send_payload_and_wait_for_ack("direct","{}",10,10,NULL,NULL));
    return 0;
}
'''
    unit = tmp_path / "fairness.c"
    unit.write_text(harness.replace("/* PRODUCTION */", source[start:end]))
    executable = tmp_path / "fairness"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(unit), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
