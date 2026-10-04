"""Failed boot cannot skip accepted writes or invent rollback compatibility."""
from test_zkt_rollback_host import MAIN, run


def test_failed_boot_coordinator_retains_uncertainty_and_original_deployment(tmp_path):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("static bool advance_failed_boot_rollback(void)\n{")
    production = source[start:source.index("static bool advance_reader_rollback(void)\n{", start)]
    contract = (MAIN / "zkt_rollback.c").read_text()
    contract = contract[contract.index("bool zj_rollback_failed_boot("):contract.index("zj_result_t zj_rollback_commit_intent(")]
    program = r'''
#include "zkt_storage_owner.h"
#include "zkt_rollback.h"
#include <assert.h>
#include <stdio.h>
typedef struct {char project_name[32],version[32];} esp_app_desc_t;
static esp_app_desc_t app={"zone_lite","2.7.0"};
static ota_journal_t s_journal,s_committed_journal;
static uint32_t s_journal_generation;
static bool s_busy,s_failed_boot_pending;
static uint64_t s_failed_boot_ticket;
static char s_last_error[64],s_running_image_digest[65];
static unsigned claims,drains,submits,polls,restarts,reports,clears;
static bool claimed,drained,admitted,complete,polled,reply_valid,cache_ok,runtime,network,clear_ok;
static zj_result_t result;
static zj_compat_result_t compatibility;
static ota_checkpoint_t accepted;
static const esp_app_desc_t *esp_app_get_description(void){return &app;}
static size_t strlcpy(char *out,const char *in,size_t n){size_t l=strlen(in);assert(l<n);memcpy(out,in,l+1);return l;}
static bool cache_running_image_digest(void){return cache_ok;}
static bool zj_runtime_boot_ready(void){return runtime;}
static bool add_connector_claim_failed_boot_restart(void){++claims;return claimed;}
bool zj_owner_quiesce(void){assert(claimed);++drains;return drained;}
bool zj_owner_select_quiesced_reader(const ota_checkpoint_t *expected,uint64_t *ticket)
{assert(claimed&&drained&&s_busy);++submits;if(!admitted)return false;accepted=*expected;*ticket=17;return true;}
bool zj_owner_poll(uint64_t ticket,zj_reply_t *reply,bool *done)
{
    assert(ticket==17&&claimed&&drained);++polls;*done=false;if(!polled)return false;if(!complete)return true;
    memset(reply,0,sizeof(*reply));reply->result=result;reply->compatibility=compatibility;
    reply->rollback_intent=accepted;++reply->rollback_intent.generation;
    strcpy(reply->rollback_intent.journal.state,"FAILED_BOOT_INTENT");
    if(!reply_valid)strcpy(reply->rollback_intent.journal.deployment_id,"different");
    reply->rollback_intent.crc=dq_crc32(&reply->rollback_intent,offsetof(ota_checkpoint_t,crc));
    *done=true;return true;
}
static void esp_restart(void){assert(claimed&&drained&&complete&&s_busy);++restarts;}
static bool report_state(const char *state,const char *error)
{assert(!strcmp(state,"ROLLED_BACK")&&runtime);
 assert(!strcmp(error,!strcmp(s_journal.state,"FAILED_BOOT_INTENT")?"BOOT_HEALTH_TIMEOUT":"PREVIOUS_FIRMWARE_OBSERVED"));
 ++reports;return network;}
static bool clear_journal(void)
{assert(network&&runtime);++clears;if(!clear_ok)return false;memset(&s_journal,0,sizeof(s_journal));s_committed_journal=s_journal;return true;}
/* CONTRACT */
/* PRODUCTION */
static void reset(void)
{
    memset(&s_journal,0,sizeof(s_journal));strcpy(s_journal.deployment_id,"original-writer-deployment");
    strcpy(s_journal.release_id,"original-writer-release");strcpy(s_journal.target_version,"2.7.0");
    strcpy(s_journal.download_url,"https://example.invalid/unused");memset(s_journal.image_sha256,'1',64);
    strcpy(s_journal.state,"READY_TO_BOOT");s_journal.image_size=s_journal.bytes_written=131072;
    s_committed_journal=s_journal;s_journal_generation=7;s_busy=s_failed_boot_pending=false;
    strcpy(app.version,"2.7.0");strcpy(app.project_name,"zone_lite");memset(s_running_image_digest,'1',64);
    claims=drains=submits=polls=restarts=reports=clears=0;s_failed_boot_ticket=0;
    claimed=drained=complete=runtime=network=false;admitted=polled=reply_valid=cache_ok=clear_ok=true;
    result=ZJ_OK;compatibility=ZJ_COMPAT_OK;
}
int main(void)
{
    reset();assert(advance_failed_boot_rollback()&&!s_busy&&!claims);
    s_failed_boot_pending=true;assert(!advance_failed_boot_rollback()&&s_busy&&claims==1&&!drains);
    claimed=true;assert(!advance_failed_boot_rollback()&&drains==1&&!submits);
    drained=true;assert(!advance_failed_boot_rollback()&&submits==1&&s_failed_boot_ticket==17);
    polled=false;for(unsigned i=0;i<100;++i)assert(!advance_failed_boot_rollback());
    assert(submits==1&&!restarts&&s_failed_boot_ticket==17);
    polled=true;complete=true;result=ZJ_UNCERTAIN;
    assert(!advance_failed_boot_rollback()&&!restarts&&!s_failed_boot_ticket);
    assert(!strcmp(s_committed_journal.state,"FAILED_BOOT_INTENT"));
    assert(!strcmp(s_committed_journal.deployment_id,"original-writer-deployment"));
    complete=false;assert(!advance_failed_boot_rollback()&&submits==2);
    complete=true;result=ZJ_OK;assert(!advance_failed_boot_rollback()&&restarts==1);
    /* Rebooted VALID bridge must recover locally before it reports rollback.
     * Lost ADD responses and failed journal cleanup retain the original intent. */
    strcpy(app.version,"2.6.16");s_failed_boot_pending=false;
    assert(!advance_failed_boot_rollback()&&!reports&&!clears);
    runtime=true;assert(!advance_failed_boot_rollback()&&reports==1&&!clears);
    network=true;clear_ok=false;assert(!advance_failed_boot_rollback()&&clears==1);
    assert(!strcmp(s_journal.state,"FAILED_BOOT_INTENT"));
    clear_ok=true;assert(advance_failed_boot_rollback()&&!s_busy&&!s_journal.deployment_id[0]);
    reset();s_failed_boot_pending=true;claimed=drained=true;
    assert(!advance_failed_boot_rollback());complete=true;reply_valid=false;
    assert(!advance_failed_boot_rollback()&&!restarts&&!strcmp(s_journal.state,"READY_TO_BOOT"));
    reset();s_failed_boot_pending=true;s_running_image_digest[0]='2';
    assert(!advance_failed_boot_rollback()&&!claims&&!submits);
    reset();s_failed_boot_pending=true;strcpy(app.version,"2.6.16");
    strcpy(s_journal.target_version,"2.6.16");
    assert(!advance_failed_boot_rollback()&&!claims&&!submits);
    assert(!strcmp(s_last_error,"BOOT_ROLLBACK_PREDECESSOR_UNQUALIFIED"));
    reset();s_failed_boot_pending=true;claimed=drained=true;admitted=false;
    assert(!advance_failed_boot_rollback()&&!s_failed_boot_ticket&&!restarts);
    /* A bootloader return before a failure-intent write still waits for the
     * real bridge reader, without inventing a reset cause or reselecting it. */
    const char *states[]={"READY_TO_BOOT","LOCAL_VALIDATED","BOOT_REPORTED","RECONCILING"};
    for(unsigned i=0;i<4;++i){
        reset();strcpy(app.version,"2.6.16");strcpy(s_journal.state,states[i]);
        assert(!advance_failed_boot_rollback()&&s_busy&&!claims&&!submits&&!reports);
        runtime=network=true;assert(advance_failed_boot_rollback()&&reports==1&&clears==1&&!s_busy);
    }
    puts("Failed-boot handoff, original identity, uncertain selection and recovery reporting passed");
}
'''
    run(tmp_path, program.replace("/* CONTRACT */", contract).replace("/* PRODUCTION */", production))


def test_hikvision_has_no_journal_failed_boot_path(tmp_path):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("static bool advance_failed_boot_rollback(void)\n{")
    production = source[start:source.index("static bool advance_reader_rollback(void)\n{", start)]
    run(tmp_path, '#include <assert.h>\n#include <stdbool.h>\n' + production +
        '\nint main(void){assert(advance_failed_boot_rollback());}\n', family=1)
