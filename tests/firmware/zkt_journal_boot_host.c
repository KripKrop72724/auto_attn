#include "zkt_journal_boot.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    zj_owner_health_t owner;
    zj_transport_health_t transport;
    bool fail_owner, fail_transport, fail_capture, fail_submit, poll_lost, complete, abandon_fails;
    unsigned owners, transports, captures, submissions, abandonments;
    uint64_t next_ticket;
    zj_compat_result_t result;
} fixture_t;
static bool start_owner(void *context, const char *serial)
{ fixture_t *f = context; assert(!strcmp(serial,"TEST-SERIAL")); ++f->owners; return !f->fail_owner; }
static bool owner_health(void *context, zj_owner_health_t *out)
{ *out = ((fixture_t *)context)->owner; return true; }
static bool start_transport(void *context)
{ fixture_t *f = context; ++f->transports; return !f->fail_transport; }
static bool transport_health(void *context, zj_transport_health_t *out)
{ *out = ((fixture_t *)context)->transport; return true; }
static bool start_capture(void *context)
{ fixture_t *f = context; ++f->captures; return !f->fail_capture; }
static bool submit(void *context, const zj_request_t *request, uint64_t *ticket)
{
    fixture_t *f = context;
    assert(request->operation == ZJ_READER_CHECK);
    ++f->submissions;
    if (f->fail_submit) return false;
    *ticket = ++f->next_ticket;
    return true;
}
static bool poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    fixture_t *f = context; assert(ticket == f->next_ticket);
    if (f->poll_lost) return false;
    *complete = f->complete;
    *reply = (zj_reply_t){.result = f->result == ZJ_COMPAT_OK ? ZJ_OK : ZJ_INVALID, .compatibility = f->result};
    return true;
}
static bool abandon(void *context, uint64_t ticket)
{ fixture_t *f = context; assert(ticket == f->next_ticket); ++f->abandonments; return !f->abandon_fails; }
static zj_boot_port_t port(fixture_t *f)
{ return (zj_boot_port_t){start_owner,owner_health,start_transport,transport_health,start_capture,submit,poll,abandon,f}; }
static zj_boot_input_t input(uint32_t now)
{ return (zj_boot_input_t){.now_ms=now,.mode=ZJ_BOOT_WRITER,.terminal_serial="TEST-SERIAL",.secure=true,.storage_ready=true,.writer_build=true}; }
static void healthy(fixture_t *f, uint32_t now)
{
    f->owner = (zj_owner_health_t){.started=true,.ready=true,.sampled_uptime_us=(uint64_t)now*1000,.compatibility=ZJ_COMPAT_NOT_READY,.delivery_authority=ZJ_AUTHORITY_LEGACY};
    f->transport = (zj_transport_health_t){.started=true,.sampled_ms=now};
}
static void step(zj_boot_t *s, fixture_t *f, zj_boot_input_t *in, uint32_t now)
{
    in->now_ms = now;
    f->owner.sampled_uptime_us = (uint64_t)now * 1000;
    f->transport.sampled_ms = now;
    zj_boot_step(s, port(f), in);
}
static void ready(zj_boot_t *s, fixture_t *f, zj_boot_input_t *in)
{
    memset(s,0,sizeof(*s)); memset(f,0,sizeof(*f)); *in=input(100);
    healthy(f,100);
    step(s,f,in,100); assert(f->owners==1);
    step(s,f,in,101); assert(f->transports==1);
    step(s,f,in,102); assert(s->ticket && f->submissions==1 && !s->writer_ready);
    f->complete=true; f->owner.compatibility_checked=true; f->owner.compatibility=ZJ_COMPAT_OK; f->owner.writer_allowed=true; f->owner.delivery_authority=ZJ_AUTHORITY_ADD;
    step(s,f,in,103); assert(!s->ticket && !s->writer_ready);
    step(s,f,in,104); assert(s->writer_ready && s->phase==ZJ_BOOT_READY && f->captures==1);
    assert(zj_boot_local_ready(s,104));
}
int main(void)
{
    /* A legacy read incident may itself require a receipt from this transport.
     * It must not prevent the already-running owner from starting recovery
     * delivery. Reader attestation and capture remain held. */
    {
        fixture_t blocked = {0}; zj_boot_t recovery = {0};
        zj_boot_input_t recovering = input(100); healthy(&blocked,100);
        recovering.storage_ready = false;
        recovery.owner_started = true;
        strcpy(recovery.terminal_serial,"TEST-SERIAL");
        step(&recovery,&blocked,&recovering,100);
        assert(blocked.transports == 1 && recovery.transport_started);
        step(&recovery,&blocked,&recovering,101);
        assert(!blocked.submissions && !blocked.captures && !recovery.reader_ready &&
            !recovery.writer_ready && !zj_boot_local_ready(&recovery,101));
        memset(&blocked,0,sizeof(blocked)); memset(&recovery,0,sizeof(recovery));
        recovering.storage_available=true; healthy(&blocked,100);
        step(&recovery,&blocked,&recovering,100);
        assert(blocked.owners==1 && recovery.owner_started);
        step(&recovery,&blocked,&recovering,101);
        assert(blocked.transports==1 && !blocked.captures && !recovery.reader_ready);
        step(&recovery,&blocked,&recovering,102);
        assert(recovery.phase==ZJ_BOOT_STORAGE_WAIT && !recovery.writer_ready && !blocked.submissions);
    }
    zj_boot_t s={0}; fixture_t f={0}; zj_boot_input_t in=input(1);
    in.mode=ZJ_BOOT_DISABLED; step(&s,&f,&in,1); assert(s.phase==ZJ_BOOT_OFF && !f.owners && zj_boot_local_ready(&s,1));
    in.mode=ZJ_BOOT_WRITER; in.secure=false; step(&s,&f,&in,2); assert(s.phase==ZJ_BOOT_SECURITY_HOLD && !f.owners);
    in.secure=true; in.terminal_serial=""; step(&s,&f,&in,3); assert(s.phase==ZJ_BOOT_BINDING_HOLD);
    in.terminal_serial="invalid serial"; step(&s,&f,&in,4); assert(s.phase==ZJ_BOOT_BINDING_HOLD);
    char too_long[82]; memset(too_long,'x',81); too_long[81]=0; in.terminal_serial=too_long;
    step(&s,&f,&in,5); assert(!f.owners);
    in.terminal_serial="TEST-SERIAL"; in.storage_ready=false; step(&s,&f,&in,6); assert(s.phase==ZJ_BOOT_STORAGE_WAIT && !f.owners);
    in.storage_ready=true; f.fail_owner=true;
    step(&s,&f,&in,0xfffffff0); assert(f.owners==1 && s.start_attempts==1 && !s.owner_starts);
    step(&s,&f,&in,100); assert(f.owners==1);
    step(&s,&f,&in,1984); assert(f.owners==2 && s.retry_delay_ms==4000);
    f.fail_owner=false; step(&s,&f,&in,5984); assert(s.owner_starts==1 && f.owners==3);
    step(&s,&f,&in,5985); assert(s.phase==ZJ_BOOT_STALLED && f.owners==3 && !f.transports);
    healthy(&f,5986); f.fail_transport=true; step(&s,&f,&in,5986); assert(f.transports==1 && !s.transport_starts);
    step(&s,&f,&in,6000); assert(f.transports==1);
    f.fail_transport=false; step(&s,&f,&in,7986); assert(s.transport_starts==1);
    f.owner.checkpoint_recovery_pending=true; step(&s,&f,&in,7987); assert(s.phase==ZJ_BOOT_RECOVERING && !s.ticket && !f.submissions);
    f.owner.checkpoint_recovery_pending=false; step(&s,&f,&in,7988); assert(s.ticket);
    f.poll_lost=true; f.abandon_fails=true;
    step(&s,&f,&in,12988); assert(s.ticket && f.abandonments==1 && !f.captures);
    f.owner.compatibility_checked=true; f.owner.compatibility=ZJ_COMPAT_OK; f.owner.writer_allowed=true; f.owner.delivery_authority=ZJ_AUTHORITY_ADD;
    f.abandon_fails=false; step(&s,&f,&in,12989); assert(!s.ticket && !f.captures);
    step(&s,&f,&in,13000); assert(!f.captures);
    step(&s,&f,&in,14989); assert(s.writer_ready && f.captures==1 && f.submissions==1);
    for(unsigned i=0;i<100;i++) step(&s,&f,&in,15000+i);
    assert(f.captures==1 && f.owners==3 && f.transports==2 && f.submissions==1);
    assert(!zj_boot_local_ready(&s,s.sampled_ms+45000));
    in.terminal_serial="REBOUND"; step(&s,&f,&in,16000); assert(s.binding_changed && !s.writer_ready);
    in.terminal_serial="TEST-SERIAL"; step(&s,&f,&in,16001); assert(s.phase==ZJ_BOOT_BINDING_HOLD && !s.writer_ready);

    ready(&s,&f,&in);
    f.owner.ready=false; step(&s,&f,&in,105); assert(s.phase==ZJ_BOOT_RECOVERING && !s.writer_ready && !zj_boot_local_ready(&s,105));
    f.owner.ready=true; f.owner.compatibility_checked=false; f.owner.writer_allowed=false;
    step(&s,&f,&in,106); assert(s.ticket);
    f.result=ZJ_COMPAT_CORRUPT; step(&s,&f,&in,107); assert(s.compatibility==ZJ_COMPAT_CORRUPT && !s.writer_ready);
    f.owner.compatibility=ZJ_COMPAT_CORRUPT; f.owner.compatibility_checked=true;
    step(&s,&f,&in,108); assert(!s.writer_ready && f.captures==1 && !zj_boot_local_ready(&s,108));

    ready(&s,&f,&in); f.owner.operation_running=true; f.owner.operation_started_us=1;
    step(&s,&f,&in,16000); assert(s.phase==ZJ_BOOT_STALLED && !s.writer_ready && f.owners==1);
    ready(&s,&f,&in); in.now_ms=46000;
    zj_boot_step(&s,port(&f),&in); assert(s.phase==ZJ_BOOT_STALLED && !s.writer_ready);
    ready(&s,&f,&in); in.writer_build=false; step(&s,&f,&in,105); assert(s.phase==ZJ_BOOT_WRITER_DISABLED && !s.writer_ready);
    ready(&s,&f,&in); f.owner.quiescing=true; step(&s,&f,&in,105);
    assert(s.phase==ZJ_BOOT_QUIESCING && !s.writer_ready && !s.reader_ready && !zj_boot_local_ready(&s,105));
    assert(!strcmp(zj_boot_phase_name(s.phase),"QUIESCING"));
    f.owner.quiesced=true;step(&s,&f,&in,106);
    assert(s.phase==ZJ_BOOT_QUIESCING && f.captures==1 && f.submissions==1);

    memset(&s,0,sizeof(s)); memset(&f,0,sizeof(f)); in=input(100); in.mode=ZJ_BOOT_BRIDGE; healthy(&f,100);
    in.bridge_validation_pending=true;
    step(&s,&f,&in,100); step(&s,&f,&in,101); step(&s,&f,&in,102);
    assert(!s.ticket && s.reader_ready && zj_boot_local_ready(&s,102) && !f.captures && !f.submissions);
    assert(s.phase==ZJ_BOOT_BRIDGE_VALIDATION);
    step(&s,&f,&in,103); assert(!s.ticket && zj_boot_local_ready(&s,103));
    in.bridge_validation_pending=false; step(&s,&f,&in,104);
    assert(s.ticket && !zj_boot_local_ready(&s,104));
    f.complete=true; f.owner.compatibility_checked=true; f.owner.compatibility=ZJ_COMPAT_OK;
    step(&s,&f,&in,105); step(&s,&f,&in,106);
    assert(s.phase==ZJ_BOOT_READY && !s.writer_ready && !f.captures && zj_boot_local_ready(&s,106));
    f.owner.compatibility=ZJ_COMPAT_CORRUPT;step(&s,&f,&in,107);
    assert(!zj_boot_local_ready(&s,107)&&!f.captures);

    /* A rollback bridge has the exact same ADD capture obligations. */
    ready(&s,&f,&in); in.mode=ZJ_BOOT_BRIDGE;
    step(&s,&f,&in,105); assert(s.writer_ready && s.delivery_authority==ZJ_AUTHORITY_ADD);
    f.owner.writer_allowed=false; step(&s,&f,&in,106);
    assert(!s.writer_ready && !zj_boot_local_ready(&s,106));
    f.owner.delivery_authority=ZJ_AUTHORITY_UNKNOWN;step(&s,&f,&in,107);
    assert(s.phase==ZJ_BOOT_AUTHORITY_HOLD && s.delivery_authority==ZJ_AUTHORITY_ADD);
    f.owner.delivery_authority=ZJ_AUTHORITY_LEGACY;step(&s,&f,&in,108);
    assert(s.phase==ZJ_BOOT_AUTHORITY_HOLD && !zj_boot_local_ready(&s,108));
    f.owner.delivery_authority=ZJ_AUTHORITY_ADD;f.owner.writer_allowed=true;step(&s,&f,&in,109);
    assert(s.writer_ready && f.captures==1);
    in.writer_build=false;step(&s,&f,&in,110);
    assert(s.phase==ZJ_BOOT_WRITER_DISABLED && !zj_boot_local_ready(&s,110));

    /* Pending bridge health is a local reader proof, never capture permission. */
    memset(&s,0,sizeof(s));memset(&f,0,sizeof(f));in=input(100);in.mode=ZJ_BOOT_BRIDGE;
    in.bridge_validation_pending=true;healthy(&f,100);f.owner.delivery_authority=ZJ_AUTHORITY_ADD;
    step(&s,&f,&in,100);step(&s,&f,&in,101);step(&s,&f,&in,102);
    assert(zj_boot_local_ready(&s,102) && !s.writer_ready && !f.captures && !f.submissions);
    in.writer_build=false;step(&s,&f,&in,103);assert(!zj_boot_local_ready(&s,103));

    memset(&s,0,sizeof(s));memset(&f,0,sizeof(f));in=input(100);healthy(&f,100);
    step(&s,&f,&in,100);step(&s,&f,&in,101);f.fail_submit=true;
    step(&s,&f,&in,102);assert(!s.ticket&&s.failures==1&&f.submissions==1);
    step(&s,&f,&in,103);assert(f.submissions==1);f.fail_submit=false;
    step(&s,&f,&in,2102);assert(s.ticket&&f.submissions==2);
    f.complete=true;f.owner.compatibility_checked=true;f.owner.compatibility=ZJ_COMPAT_OK;f.owner.writer_allowed=true; f.owner.delivery_authority=ZJ_AUTHORITY_ADD;
    step(&s,&f,&in,2103);f.fail_capture=true;step(&s,&f,&in,2104);
    assert(f.captures==1&&!s.capture_started&&!s.writer_ready);
    step(&s,&f,&in,2105);assert(f.captures==1);f.fail_capture=false;step(&s,&f,&in,4104);
    assert(f.captures==2&&s.capture_starts==1&&s.writer_ready&&f.owners==1&&f.transports==1);
    assert(!strcmp(zj_boot_phase_name((zj_boot_phase_t)99),"UNKNOWN"));
    puts("journal startup, retry, timeout, recovery and binding tests passed");
}
