"""Exercise signed-bridge manifest handoff without changing its firmware image.

A shorter job's committed receipt is not permission to replace the active tail.
The backend's negative transport ACK leaves the existing checkpoint untouched;
its separate source_coverage message is applied by the next gateway iteration.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_committed_short_manifest_handoff_preserves_tail_between_gateway_iterations(tmp_path):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    cjson = idf / "components/json/cJSON"
    if not (cjson / "cJSON.c").is_file():
        pytest.skip("Pinned ESP-IDF cJSON source is required")
    main = ROOT / "firmware/zone_lite/main"
    zone = (main / "zone_lite.c").read_text()
    connector = (main / "add_connector.c").read_text()
    completion = _block(zone, "    if (assignment->has_cutoff &&\n        assignment->committed_next_ordinal == cutoff)")
    apply_coverage = _block(zone, "static bool apply_add_source_coverage(")
    release = _block(zone, "static void release_reconciliation_credit(")
    error = _block(connector, '    if (cJSON_IsString(type) && strcmp(type->valuestring, "error") == 0)')
    coverage_message = _block(connector, '    if (cJSON_IsString(type) && strcmp(type->valuestring, "source_coverage") == 0)')
    # The signed gateway checks the queue before its assignment and can run a
    # tail later in that same iteration. Do not pretend a queued message has
    # already updated the checkpoint while the assignment's ACK waiter wakes.
    queue_at = zone.index("        add_source_coverage_t authoritative_coverage;")
    assignment_at = zone.index("        add_reconcile_assignment_t reconciliation_assignment;", queue_at)
    tail_at = zone.index("process_add_source_tail_step(sock, &ctx, users, &more)", assignment_at)
    assert queue_at < assignment_at < tail_at
    consume = zone[queue_at:assignment_at]
    program = r'''
#include "add_source_wire.h"
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static const char *TAG="test";
static char g_device_serial[]="SYNTHETIC-TERMINAL",g_add_source_epoch[37];
static char g_add_source_coverage_chain[65],durable_chain[65];
static uint32_t g_add_source_coverage_cursor,g_add_source_coverage_generation,durable_cursor;
static bool g_add_source_coverage_certified,g_force_truth_reconcile,g_history_backfill_pending;
static bool g_history_backfill_had_failures,g_temp_admin_active,commit_failed;
static int32_t g_last_synced_attendance_count;
static struct {bool add_source_coverage_certified;uint32_t add_source_coverage_cursor;} g_add_zkt;
static char s_waiting_ack[80];
static bool s_ack_matched,queued;
static unsigned commits,applied,releases,wakes;
static int s_lock=1,s_ack_sem=2,s_source_coverage=3;
static add_source_coverage_t pending;
static const char epoch[]="11111111-2222-4333-8444-555555555555";
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
static int xSemaphoreTake(int sem,int ticks){(void)sem;(void)ticks;return 1;}
static void xSemaphoreGive(int sem){if(sem==s_ack_sem)++wakes;}
static int xQueueOverwrite(int queue,const void *value){assert(queue==s_source_coverage);pending=*(const add_source_coverage_t *)value;queued=true;return 1;}
bool add_connector_take_source_coverage(add_source_coverage_t *out){if(!queued)return false;*out=pending;queued=false;return true;}
static void quiet_log(const char *tag,const char *fmt,...){(void)tag;(void)fmt;}
#define ESP_LOGW quiet_log
static bool zkt_source_epoch_required(void){return true;}
static bool source_epoch_required(void){return true;}
static void copy_string(char *to,const char *from,size_t capacity){size_t n=strlen(from);if(n>=capacity)n=capacity-1;memcpy(to,from,n);to[n]=0;}
#define strlcpy copy_string
static bool nvs_save_runtime_state(void){++commits;if(commit_failed){g_add_source_coverage_certified=false;g_add_zkt.add_source_coverage_certified=false;return false;}durable_cursor=g_add_source_coverage_cursor;strcpy(durable_chain,g_add_source_coverage_chain);return true;}
bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *text){(void)level;(void)subsystem;(void)text;if(!strcmp(code,"ADD_SOURCE_COVERAGE_APPLIED"))++applied;return true;}
static void zkt_preserve_first_source_boundary(int sock,void *ctx){(void)sock;(void)ctx;}
static void receive(cJSON *root){cJSON *type=cJSON_GetObjectItemCaseSensitive(root,"type");
/* ERROR_HANDLER */
/* COVERAGE_HANDLER */
assert(!"unexpected synthetic message");}
static void receive_error(const char *message_id){cJSON *root=cJSON_CreateObject();cJSON_AddStringToObject(root,"type","error");cJSON_AddStringToObject(root,"message_id",message_id);cJSON_AddStringToObject(root,"message_type","reconcile_source_manifest");cJSON_AddStringToObject(root,"code","SOURCE_COVERAGE_RETAINED");receive(root);}
static void receive_authority(uint32_t cursor,char digest){cJSON *root=cJSON_CreateObject();char chain[65];memset(chain,digest,64);chain[64]=0;cJSON_AddStringToObject(root,"type","source_coverage");cJSON_AddStringToObject(root,"terminal_serial",g_device_serial);cJSON_AddStringToObject(root,"source_epoch",epoch);cJSON_AddNumberToObject(root,"terminal_generation",5);cJSON_AddNumberToObject(root,"source_committed_cursor",cursor);cJSON_AddStringToObject(root,"source_committed_chain_digest",chain);cJSON_AddBoolToObject(root,"active",true);receive(root);}
enum reply_mode {REJECT_ONLY,AUTHORITY_BEFORE_REJECT,AUTHORITY_AFTER_REJECT,LOST_REPLY,NORMAL_CURRENT_ACK};
static enum reply_mode reply;
bool add_connector_send_payload_acknowledged(const char *type,const char *json,uint32_t timeout){
 assert(!strcmp(type,"reconcile_source_manifest") && timeout==30000);
 cJSON *request=cJSON_Parse(json);assert(request);char supplied[37];assert(add_source_epoch_read(request,supplied,true) && !strcmp(supplied,epoch));cJSON_Delete(request);
 strcpy(s_waiting_ack,"test-manifest");s_ack_matched=false;
 if(reply==NORMAL_CURRENT_ACK){cJSON *ack=cJSON_CreateObject();cJSON_AddStringToObject(ack,"type","reconcile_manifest_ack");cJSON_AddStringToObject(ack,"source_epoch",epoch);bool ok=add_source_ack_matches(ack,add_source_ack_type(type),epoch);cJSON_Delete(ack);return ok;}
 if(reply==LOST_REPLY)return false;
 if(reply==AUTHORITY_BEFORE_REJECT)receive_authority(4513,'c');
 receive_error("unrelated-message");assert(s_waiting_ack[0] && !s_ack_matched);
 unsigned before=wakes;receive_error("test-manifest");assert(!s_waiting_ack[0] && !s_ack_matched && wakes==before+1);
 if(reply==AUTHORITY_AFTER_REJECT)receive_authority(4513,'c');
 return s_ack_matched;
}
bool add_connector_send_payload(const char *type,const char *json){assert(!strcmp(type,"reconcile_assignment_release"));cJSON *body=cJSON_Parse(json);assert(body);assert(cJSON_GetObjectItemCaseSensitive(body,"committed_next_ordinal")->valuedouble==3172);assert(!strcmp(cJSON_GetObjectItemCaseSensitive(body,"reason")->valuestring,"TRANSIENT_STEP_FAILED"));char supplied[37];assert(add_source_epoch_read(body,supplied,true) && !strcmp(supplied,epoch));cJSON_Delete(body);++releases;return true;}
/* RELEASE */
static bool complete_manifest(const add_reconcile_assignment_t *assignment,int32_t latest_records){uint32_t cutoff=assignment->cutoff_count;
/* COMPLETE */
return false;}
/* APPLY */
static void next_gateway_iteration(void){int sock=0,ctx=0;
/* CONSUME */
}
static void reset_state(void){strcpy(g_add_source_epoch,epoch);g_add_source_coverage_certified=true;g_add_zkt.add_source_coverage_certified=true;g_add_source_coverage_cursor=g_add_zkt.add_source_coverage_cursor=durable_cursor=4512;g_add_source_coverage_generation=5;memset(g_add_source_coverage_chain,'b',64);g_add_source_coverage_chain[64]=0;strcpy(durable_chain,g_add_source_coverage_chain);queued=false;commit_failed=false;commits=applied=releases=wakes=0;g_force_truth_reconcile=g_history_backfill_pending=true;}
static void assert_prior_authority(void){assert(g_add_source_coverage_certified && g_add_source_coverage_cursor==4512 && durable_cursor==4512 && g_add_zkt.add_source_coverage_cursor==4512);assert(g_add_source_coverage_chain[0]=='b' && durable_chain[0]=='b');assert(commits==0 && g_force_truth_reconcile && g_history_backfill_pending);}
int main(void){
 add_reconcile_assignment_t assignment={.has_cutoff=true,.cutoff_count=3172,.committed_next_ordinal=3172,.generation=5,.stream_v2=true};strcpy(assignment.source_epoch,epoch);strcpy(assignment.job_id,"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");strcpy(assignment.assignment_id,"ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee");memset(assignment.preceding_chain_digest,'a',64);
 for(reply=REJECT_ONLY;reply<=LOST_REPLY;reply++){
  reset_state();next_gateway_iteration();assert(!complete_manifest(&assignment,4513));
  /* A same-iteration tail sees the old valid authority, never cutoff3172. */
  assert_prior_authority();release_reconciliation_credit(&assignment,assignment.committed_next_ordinal,"TRANSIENT_STEP_FAILED");assert(releases==1);
  bool had_queued=queued;next_gateway_iteration();
  if(had_queued){assert(g_add_source_coverage_cursor==4513 && durable_cursor==4513 && applied==1);assert(g_add_source_coverage_chain[0]=='c');}
  else{assert_prior_authority();receive_authority(4513,'c');next_gateway_iteration();assert(g_add_source_coverage_cursor==4513 && durable_cursor==4513);}
  /* Lost receipt replay remains a handoff, with no second cursor rewrite. */
  unsigned saved=commits;assert(!complete_manifest(&assignment,4513));assert(g_add_source_coverage_cursor==4513 && durable_cursor==4513 && commits==saved);next_gateway_iteration();assert(g_add_source_coverage_cursor==4513);
 }
 reset_state();reply=AUTHORITY_AFTER_REJECT;assert(!complete_manifest(&assignment,4513));commit_failed=true;next_gateway_iteration();assert(!g_add_source_coverage_certified && !g_add_source_epoch[0] && durable_cursor==4512 && !applied);commit_failed=false;receive_authority(4513,'c');next_gateway_iteration();assert(g_add_source_coverage_certified && durable_cursor==4513 && applied==1);
 /* A normal ACK is still valid when the job exactly owns current authority. */
 reset_state();reply=NORMAL_CURRENT_ACK;assignment.cutoff_count=assignment.committed_next_ordinal=4512;memset(assignment.preceding_chain_digest,'b',64);assert(complete_manifest(&assignment,4513));assert(g_add_source_coverage_cursor==4512 && durable_cursor==4512 && commits==1);
 return 0;
}
'''
    for marker, content in (("ERROR_HANDLER", error), ("COVERAGE_HANDLER", coverage_message),
                            ("RELEASE", release), ("COMPLETE", completion),
                            ("APPLY", apply_coverage), ("CONSUME", consume)):
        program = program.replace(f"/* {marker} */", content)
    unit = tmp_path / "handoff.c"
    unit.write_text(program)
    binary = tmp_path / "handoff"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-deprecated-declarations", "-fsanitize=address,undefined", "-I", str(main), "-I", str(cjson),
                    str(unit), str(main / "add_source_wire.c"), str(cjson / "cJSON.c"),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
