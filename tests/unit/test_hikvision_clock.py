from sqlalchemy import select
from zk_add.hikvision_evidence import preserve_observation
from zk_add.models import AttendanceEvent, DeviceAlert
from zk_add.schemas import HeartbeatPayload
from zk_add.service import update_heartbeat
from test_hikvision_delivery import payload, policy
from test_hikvision_evidence import db as evidence_db

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from zk_add.hikvision_clock import HikvisionClockSample, clock_evidence


@pytest.fixture
def db(monkeypatch):
    yield from evidence_db.__wrapped__(monkeypatch)


@pytest.mark.parametrize("drift,quality", [(0,"OK"), (-90,"OK"), (120,"OK"), (121,"DRIFTED"), (-150,"DRIFTED")])
def test_recent_offset_bearing_punch_uses_measured_drift(drift, quality):
    sample = HikvisionClockSample(device_epoch=1790000000+drift, sampled_epoch=1790000000)
    event = datetime.fromtimestamp(sample.device_epoch-2, timezone.utc)
    assert clock_evidence(sample, event, 1790000005, "POLL", False) == (quality,drift)


@pytest.mark.parametrize("age,event_age,channel,assumed", [(121,0,"POLL",False),(-1,0,"POLL",False),
 (1,121,"POLL",False),(1,0,"HISTORY",False),(1,0,"POLL",True)])
def test_missing_contemporaneous_proof_stays_unknown(age,event_age,channel,assumed):
    sample = HikvisionClockSample(device_epoch=1790000000, sampled_epoch=1790000000)
    event = datetime.fromtimestamp(1790000000-event_age,timezone.utc)
    assert clock_evidence(sample,event,1790000000+age,channel,assumed) == ("UNKNOWN",None)
    assert clock_evidence(None,event,1790000000,"POLL",False) == ("UNKNOWN",None)


@pytest.mark.parametrize("value", [True, 1790000000.5, "1790000000", 0])
def test_clock_sample_rejects_invalid_epochs(value):
    with pytest.raises(ValidationError):
        HikvisionClockSample(device_epoch=value,sampled_epoch=1790000000)



def test_clock_proof_is_retained_with_attendance_and_replay_is_immutable(db):
    session, connector = db
    policy(session, connector)
    epoch = 1790000000
    sample = HikvisionClockSample(device_epoch=epoch-90, sampled_epoch=epoch)
    message = payload(time=datetime.fromtimestamp(epoch-90,timezone.utc).isoformat())
    message.clock_sample = sample
    preserve_observation(session,connector,message)
    row = session.scalar(select(AttendanceEvent))
    assert row.clock_quality == 'OK' and row.clock_drift_seconds == -90
    assert row.raw_event['clock_sample'] == sample.model_dump()
    assert row.raw_event['clock_verification'] == 'CONTEMPORANEOUS_SAMPLE'
    message.clock_sample = None
    preserve_observation(session,connector,message)
    assert row.clock_quality == 'OK' and row.clock_drift_seconds == -90


def heartbeat(clock=None, durability='HEALTHY'):
    return HeartbeatPayload(firmware_family='hikvision',uptime_seconds=100,led_state='HEALTHY',
        terminal=dict(schema_version=2,vendor='hikvision',protocol='isapi',serial='terminal',ip_address='192.0.2.1',
                      capability_profile='pilot',qualification_state='NOT_QUALIFIED',online=True,connection_state='ONLINE',
                      stream_open=False,stream_error=0,current_event_count=0,replay_event_count=0,last_stream_message_epoch=0,
                      source_storage_failures=0,source_queue_depth=0,capture_mode='poll',poll_interval_seconds=2,clock_sample=clock),
        diagnostics=dict(storage=dict(durability=durability,persistence_verified=durability=='HEALTHY',recovery_complete=True),
                         workers=[dict(name=n,state='RUNNING',last_activity_uptime_ms=100000) for n in ['add_delivery','hikvision_source']]))


def test_hikvision_recovery_clears_fault_only_after_proof_and_clock_expires(db):
    session, connector = db
    now=int(datetime.now(timezone.utc).timestamp())
    def update(p,seq):
        update_heartbeat(session,connector=connector,boot_id='clock-health',sequence=seq,payload=p)
        session.flush()
    update(heartbeat(durability='DEGRADED'),1)
    assert connector.lifecycle_state=='DEGRADED'
    update(heartbeat(durability='UNKNOWN'),2)
    assert connector.lifecycle_state=='DEGRADED'
    update(heartbeat(dict(device_epoch=now-90,sampled_epoch=now)),3)
    assert connector.lifecycle_state=='ONLINE' and connector.last_error_code is None
    assert connector.zkt_device.device_time_drift_seconds==-90
    assert session.scalar(select(DeviceAlert).where(DeviceAlert.code=='ESP_DURABILITY_FAULT')).state=='RESOLVED'
    update(heartbeat(dict(device_epoch=now-150,sampled_epoch=now)),4)
    assert session.scalar(select(DeviceAlert).where(DeviceAlert.code=='HIK_CLOCK_DRIFT')).state=='OPEN'
    update(heartbeat(),5)
    assert connector.zkt_device.sampled_device_time is None
    update(heartbeat(dict(device_epoch=now-10,sampled_epoch=now)),6)
    assert session.scalar(select(DeviceAlert).where(DeviceAlert.code=='HIK_CLOCK_DRIFT')).state=='RESOLVED'

@pytest.mark.parametrize('error,code', [(1,'HIK_CONFIGURATION'), (2,'HIK_NETWORK'), (3,'HIK_AUTH'),
 (4,'HIK_HTTP_STATUS'), (5,'HIK_OVERSIZED'), (6,'HIK_SOURCE_CHANGED'), (7,'HIK_INVALID_RESPONSE'), (8,'HIK_STORAGE')])
def test_hik_terminal_failure_explained_despite_live_heartbeat(db,error,code):
    session,connector=db
    p=heartbeat()
    p.terminal.online=False
    p.terminal.connection_state='OFFLINE'
    p.terminal.poll_error=error
    update_heartbeat(session,connector=connector,boot_id='terminal-fault',sequence=1,payload=p)
    assert connector.connected
    assert connector.lifecycle_state=='DEGRADED'
    assert connector.last_error_code==code
    assert connector.last_error_message
    p.terminal.online=True
    p.terminal.connection_state='ONLINE'
    p.terminal.poll_error=0
    update_heartbeat(session,connector=connector,boot_id='terminal-fault',sequence=2,payload=p)
    assert connector.lifecycle_state=='ONLINE'
    assert connector.last_error_code is None
