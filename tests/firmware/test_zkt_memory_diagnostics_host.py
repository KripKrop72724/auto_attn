"""Exercise the production ZKT diagnostic hook with allocation-forbidden callbacks."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"

HARNESS = r'''
#include <assert.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_heap_caps.h"
static _Thread_local int inside_hook;
static esp_alloc_failed_hook_t registered;
static atomic_uint registrations, heap_reads, timer_reads, log_calls;
static atomic_uint workers_done;
static int registration_result;
static int64_t clock_us;
static char last_header[1024];
static unsigned sample_lines, last_sample_bytes, last_clamped;
static int stress, nested_report;
static volatile atomic_uint *forced_counter;
static unsigned forced_cas_calls;
static int checked_cas(volatile atomic_uint *word, unsigned *expected, unsigned desired,
    memory_order success, memory_order failure)
{
    if(word==forced_counter){++forced_cas_calls;*expected=atomic_load_explicit(word,failure);return 0;}
    return atomic_compare_exchange_strong_explicit(word,expected,desired,success,failure);
}
int heap_caps_register_failed_alloc_callback(esp_alloc_failed_hook_t callback)
{ assert(!inside_hook); atomic_fetch_add(&registrations,1); registered=callback; return registration_result; }
size_t heap_caps_get_free_size(uint32_t caps)
{
    assert(!inside_hook); atomic_fetch_add(&heap_reads,1);
    if(caps==MALLOC_CAP_DMA)return 16000;
    if(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT))return 32000;
    assert(caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));return 4000000;
}
size_t heap_caps_get_largest_free_block(uint32_t caps)
{
    assert(!inside_hook); atomic_fetch_add(&heap_reads,1);
    if(caps==MALLOC_CAP_DMA)return 1600;
    if(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT))return 3200;
    assert(caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));return 2000000;
}
int64_t esp_timer_get_time(void)
{ assert(!inside_hook); atomic_fetch_add(&timer_reads,1);return clock_us; }
void mock_log(const char *format,...);
#undef atomic_compare_exchange_strong_explicit
#define atomic_compare_exchange_strong_explicit checked_cas
#include "zkt_memory_diagnostics.c"
void mock_log(const char *format,...)
{
    assert(!inside_hook); atomic_fetch_add(&log_calls,1);
    char out[1024];va_list args;va_start(args,format);
    int length=vsnprintf(out,sizeof(out),format,args);va_end(args);
    assert(length>0 && (size_t)length<sizeof(out));
    assert(strstr(out,"secret")==NULL);
    if(strstr(out,"stage=")){
        strcpy(last_header,out);
        if(nested_report){nested_report=0;zkt_memory_diag_report(ZMD_STORAGE_INIT_RETURNED,0);}
    }else{
        unsigned seq,bytes,caps,clamped,exact;
        assert(sscanf(out,"ZMD1 sample_seq=%u bytes=%u caps=%u size_clamped=%u sequence_exact=%u",
            &seq,&bytes,&caps,&clamped,&exact)==5);
        assert(exact<=1);
        ++sample_lines;last_sample_bytes=bytes;last_clamped=clamped;
        if(stress)assert(caps==(bytes^0xa55aU) && clamped==0);
    }
}
static void fail(size_t bytes,uint32_t caps)
{
    inside_hook=1;
    /* Invalid pointer is intentional: the callback must not read/copy names. */
    registered(bytes,caps,(const char *)(uintptr_t)1);
    inside_hook=0;
}
static void basic(void)
{
    zkt_memory_diag_report(ZMD_BOOT,0);assert(atomic_load(&log_calls)==0);
    for(unsigned i=0;i<10;i++)zkt_memory_diag_init();
    assert(atomic_load(&registrations)==1 && registered);
    for(unsigned i=0;i<4;i++)fail(1600+i,MALLOC_CAP_DMA);
    fail(777,MALLOC_CAP_DMA);
    assert(atomic_load(&heap_reads)==0 && atomic_load(&timer_reads)==0 && atomic_load(&log_calls)==0);
    assert(atomic_load(&failures_mod32)==5 && atomic_load(&dropped_mod32)==1);
    zkt_memory_diag_report(ZMD_BOOT,0);
    assert(atomic_load(&heap_reads)==6 && sample_lines==4);
    assert(strstr(last_header,"fail_mod32=5 drop_mod32=1 samples=4"));
    assert(strstr(last_header,"dma_free=16000 dma_largest=1600"));
    assert(strstr(last_header,"int8_free=32000 int8_largest=3200"));
    assert(strstr(last_header,"psram8_free=4000000 psram8_largest=2000000"));
    unsigned logs=atomic_load(&log_calls);
    zkt_memory_diag_report(ZMD_BOOT,99);zkt_memory_diag_report((zmd_stage_t)-1,99);
    zkt_memory_diag_report(ZMD_STAGE_COUNT,99);assert(atomic_load(&log_calls)==logs);
    fail(42,MALLOC_CAP_INTERNAL);zkt_memory_diag_report(ZMD_STORAGE_INIT_RETURNED,0);
    assert(sample_lines==5 && last_sample_bytes==42 && last_clamped==0);
}
static void rate_limit(void)
{
    zkt_memory_diag_init();clock_us=0;
    zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,123);assert(atomic_load(&log_calls)==1);
    clock_us=59999999;
    zkt_memory_diag_report(ZMD_ORDS_TRANSPORT_FAILURE,456);assert(atomic_load(&log_calls)==1);
    clock_us=60000000;
    zkt_memory_diag_report(ZMD_ORDS_TRANSPORT_FAILURE,456);assert(atomic_load(&log_calls)==2);
    assert(strstr(last_header,"err=456"));
    clock_us=1;zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,1);
    clock_us=-1;zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,1);
    assert(atomic_load(&log_calls)==2);
    clock_us=120000000;zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,1);
    assert(atomic_load(&log_calls)==3);
    /* Startup stages are independent and emit only once even during failures. */
    zkt_memory_diag_report(ZMD_BOOT,0);zkt_memory_diag_report(ZMD_BOOT,0);
    assert(atomic_load(&log_calls)==4);
}
static void wrapping(void)
{
    zkt_memory_diag_init();atomic_store(&failures_mod32,UINT_MAX);
    fail(1,8);fail(2,8);assert(atomic_load(&failures_mod32)==1);
    fail(3,8);fail(4,8);atomic_store(&dropped_mod32,UINT_MAX);
    fail(5,8);assert(atomic_load(&dropped_mod32)==0);
    zkt_memory_diag_report(ZMD_BOOT,0);assert(sample_lines==4);
    if(sizeof(size_t)>4){fail((size_t)UINT32_MAX+1,8);
        zkt_memory_diag_report(ZMD_STORAGE_INIT_RETURNED,0);
        assert(last_sample_bytes==UINT32_MAX && last_clamped==1);}
}
static void *producer(void *arg)
{
    unsigned base=(unsigned)(uintptr_t)arg;
    for(unsigned i=1;i<=20000;i++)fail(base+i,(base+i)^0xa55aU);
    atomic_fetch_add(&workers_done,1);return NULL;
}
static void *initializer(void *arg)
{(void)arg;for(unsigned i=0;i<1000;i++)zkt_memory_diag_init();return NULL;}
static void concurrent(void)
{
    pthread_t init[8];
    for(unsigned i=0;i<8;i++)assert(!pthread_create(&init[i],NULL,initializer,NULL));
    for(unsigned i=0;i<8;i++)assert(!pthread_join(init[i],NULL));
    assert(atomic_load(&registrations)==1);stress=1;
    pthread_t producers[4];
    for(unsigned i=0;i<4;i++)assert(!pthread_create(&producers[i],NULL,producer,(void *)(uintptr_t)(i*20000)));
    while(atomic_load(&workers_done)<4){
        clock_us+=60000000;zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,0);
    }
    for(unsigned i=0;i<4;i++)assert(!pthread_join(producers[i],NULL));
    clock_us+=60000000;zkt_memory_diag_report(ZMD_ADD_TRANSPORT_FAILURE,0);
    unsigned failures=atomic_load(&failures_mod32), dropped=atomic_load(&dropped_mod32);
    assert(failures<=80000 && sample_lines+dropped<=80000);
    if(!atomic_load(&count_incomplete))assert(failures==80000 && sample_lines+dropped==80000);
    for(unsigned i=0;i<4;i++)assert(atomic_load(&slots[i].state)==0);
}
static void recursive_report(void)
{
    zkt_memory_diag_init();nested_report=1;zkt_memory_diag_report(ZMD_BOOT,0);
    assert(atomic_load(&log_calls)==1);
    zkt_memory_diag_report(ZMD_STORAGE_INIT_RETURNED,0);assert(atomic_load(&log_calls)==2);
}
static void bounded_contention(void)
{
    zkt_memory_diag_init();forced_counter=&failures_mod32;
    fail(1600,8);
    assert(forced_cas_calls==ZMD_COUNTER_ATTEMPTS);
    assert(atomic_load(&failures_mod32)==0 && atomic_load(&count_incomplete)==1);
    assert(atomic_load(&slots[0].state)==2 && slots[0].value.sequence_exact==0);
    forced_counter=NULL;zkt_memory_diag_report(ZMD_BOOT,0);
    assert(sample_lines==1 && strstr(last_header,"count_incomplete=1"));
    for(unsigned i=0;i<4;i++)fail(i+1,8);
    forced_cas_calls=0;forced_counter=&dropped_mod32;fail(999,8);
    assert(forced_cas_calls==ZMD_COUNTER_ATTEMPTS && atomic_load(&dropped_mod32)==0);
    forced_counter=NULL;zkt_memory_diag_report(ZMD_STORAGE_INIT_RETURNED,0);
    assert(strstr(last_header,"count_incomplete=1"));
}
int main(int argc,char **argv)
{
    assert(argc==2);
    if(!strcmp(argv[1],"basic"))basic();
    else if(!strcmp(argv[1],"rate"))rate_limit();
    else if(!strcmp(argv[1],"wrap"))wrapping();
    else if(!strcmp(argv[1],"concurrent"))concurrent();
    else if(!strcmp(argv[1],"recursive"))recursive_report();
    else if(!strcmp(argv[1],"contention"))bounded_contention();
    else{assert(!strcmp(argv[1],"registration_failure"));registration_result=7;
        zkt_memory_diag_init();zkt_memory_diag_init();zkt_memory_diag_report(ZMD_BOOT,0);
        assert(atomic_load(&registrations)==1 && strstr(last_header,"hook=7"));}
    return 0;
}
'''


@pytest.fixture(scope="module")
def diagnostic_binary(tmp_path_factory):
    directory = tmp_path_factory.mktemp("zkt-memory-diagnostics")
    (directory / "esp_attr.h").write_text("#define IRAM_ATTR\n#define DRAM_ATTR\n")
    (directory / "esp_heap_caps.h").write_text("""#pragma once
#include <stddef.h>
#include <stdint.h>
#define MALLOC_CAP_8BIT 4
#define MALLOC_CAP_DMA 8
#define MALLOC_CAP_SPIRAM 1024
#define MALLOC_CAP_INTERNAL 2048
typedef void (*esp_alloc_failed_hook_t)(size_t,uint32_t,const char *);
int heap_caps_register_failed_alloc_callback(esp_alloc_failed_hook_t);
size_t heap_caps_get_free_size(uint32_t);
size_t heap_caps_get_largest_free_block(uint32_t);
""")
    (directory / "esp_timer.h").write_text("#include <stdint.h>\nint64_t esp_timer_get_time(void);\n")
    (directory / "esp_log.h").write_text('#define ESP_LOGI(tag, ...) mock_log(__VA_ARGS__)\n')
    source = directory / "diagnostics.c"
    source.write_text(HARNESS)
    binary = directory / "diagnostics"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
        "-DESP_PLATFORM=1", "-DZONE_LITE_HIKVISION=0", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-pthread", "-I", str(directory), "-I", str(MAIN),
        str(source), "-o", str(binary),
    ], check=True)
    return binary


@pytest.mark.parametrize("case", ["basic", "rate", "wrap", "concurrent", "recursive", "contention", "registration_failure"])
def test_production_memory_diagnostics(diagnostic_binary, case):
    subprocess.run([str(diagnostic_binary), case], check=True, timeout=30)


def test_hikvision_needs_no_sdk_or_diagnostic_registration(tmp_path):
    source = tmp_path / "hikvision.c"
    source.write_text('''#include "zkt_memory_diagnostics.h"
int main(void) { zkt_memory_diag_init(); zkt_memory_diag_report(ZMD_BOOT, 1); return 0; }
''')
    binary = tmp_path / "hikvision"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-DESP_PLATFORM=1", "-DZONE_LITE_HIKVISION=1", "-I", str(MAIN),
        str(source), str(MAIN / "zkt_memory_diagnostics.c"), "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)


def test_allocation_hook_has_one_owner_in_firmware():
    # IDF offers no getter/chaining API. A new component must not silently
    # replace this sole project registration, or this test must be redesigned.
    owners = [path.name for path in MAIN.glob("*.c")
              if "heap_caps_register_failed_alloc_callback(" in path.read_text()]
    assert owners == ["zkt_memory_diagnostics.c"]
