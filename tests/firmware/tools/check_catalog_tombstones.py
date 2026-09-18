"""Actual tombstone loader/update: every allocation failure preserves the catalog."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
source = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
functions = source[source.index("static cJSON *load_catalog_for_tombstone("):
                   source.index("static bool append_cancelled_command(")]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/stat.h>
#include "cJSON.h"
#define ADD_IDENTITY_CATALOG_PATH "catalog"
#define ADD_IDENTITY_CATALOG_MAX_BYTES (2U*1024U*1024U)
#define ADD_IDENTITY_CATALOG_MAX_ROWS 8192
#define ADD_COMMAND_LINE_BYTES 12288
static size_t calls,fail_at,persists;
static bool fail_close;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
static int close_file(FILE *file){int result=fclose(file);return fail_close?EOF:result;}
#define malloc allocate
#define fclose close_file
static char *decrypt_storage_line(const char *line){char *p=malloc(strlen(line)+1);if(p)strcpy(p,line);return p;}
typedef struct {bool has_tombstone;char user_id[32],uid[32],tombstone_display_name[32],tombstone_cnic[32];bool tombstone_shift_worker;} add_command_t;
static bool persist_identity_catalog_locked(cJSON *root,size_t *count){
 (void)count;persists++;
 cJSON *rows=cJSON_GetObjectItemCaseSensitive(root,"rows");assert(cJSON_GetArraySize(rows)==2);
 cJSON *old=cJSON_GetArrayItem(rows,0),*new=cJSON_GetArrayItem(rows,1);
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(old,"uid")->valuestring,"old"));
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(new,"uid")->valuestring,"new"));return true;
}
static int catalog_lock;
static int *s_catalog_lock=&catalog_lock;
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
static int xSemaphoreTake(int *lock,unsigned timeout){(void)timeout;assert(!*lock);*lock=1;return 1;}
static void xSemaphoreGive(int *lock){assert(*lock);*lock=0;}
static bool recover_catalog_transaction_locked(void){assert(catalog_lock);return true;}
''' + functions + r'''
static void seed(const char *data){FILE *f=fopen("catalog","w");assert(f && fputs(data,f)>=0);assert(!fclose(f));}
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 add_command_t command={.has_tombstone=true,.user_id="reused-id",.uid="new",.tombstone_display_name="Test",.tombstone_cnic="test-identity"};
 seed("{\"rows_count\":1}\n{\"user_id\":\"reused-id\",\"uid\":\"old\"}\n");
 assert(add_connector_persist_command_tombstone(&command));size_t total=calls;
 for(size_t i=1;i<=total;i++){
  calls=0;fail_at=i;persists=0;assert(!add_connector_persist_command_tombstone(&command));assert(!persists);
 }
 fail_at=0;fail_close=true;persists=0;assert(!add_connector_persist_command_tombstone(&command) && !persists);fail_close=false;
 const char *bad[]={"", "{\"rows_count\":2}\n{\"uid\":\"old\"}\n", "{\"rows_count\":0}\n{}\n", "{\"rows_count\":1}\n{\"uid\":\"old\"}", "{\"rows\":[]}\n{}\n"};
 for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++){seed(bad[i]);persists=0;assert(!add_connector_persist_command_tombstone(&command) && !persists);}
 puts("catalog tombstone allocation regressions passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "catalog_tombstones.c"
    unit.write_text(program)
    executable = temporary / "catalog-test"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(cjson),
                    str(unit), str(cjson / "cJSON.c"), "-lm", "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True)
