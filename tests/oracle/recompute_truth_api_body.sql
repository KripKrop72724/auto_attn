-- Minimal SLIC_ZKT_TRUTH_API package body for the recompute proof
-- (tests/oracle/run_recompute_checks.py). The three helper functions below are
-- copied verbatim from production SLIC_HRM.SLIC_ZKT_TRUTH_API (package body lines
-- 8 and 133-175, read 13 Nov 2026 via USER_SOURCE; whitespace normalised by the
-- read). The runner also requires them to match the checked-in installer
-- deploy/add/oracle/slic_zkt_truth_api.sql.
create or replace package body slic_zkt_truth_api as
c_attendance_timezone constant varchar2(64) := 'Asia/Karachi';

function parse_event_timestamp(p_value in varchar2) return timestamp with time zone is
    v_value varchar2(80) := trim(p_value);
begin
    if v_value is null then
        return null;
    end if;

    v_value := regexp_replace(v_value, 'Z$', '+00:00', 1, 0, 'i');

    if regexp_like(v_value, '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}$') then
        return to_timestamp_tz(v_value, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM') at time zone c_attendance_timezone;
    elsif regexp_like(v_value, '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$') then
        return from_tz(to_timestamp(v_value, 'YYYY-MM-DD HH24:MI:SS'), c_attendance_timezone);
    end if;

    return null;
exception
    when others then
        return null;
end parse_event_timestamp;

function attendance_timestamp_is_plausible(
    p_event_ts in timestamp with time zone
) return number is
begin
    if p_event_ts is null
       or p_event_ts < to_timestamp_tz(
           '2010-01-01 00:00:00 Asia/Karachi',
           'YYYY-MM-DD HH24:MI:SS TZR'
       )
       or p_event_ts > systimestamp + interval '1' day then
        return 0;
    end if;
    return 1;
end attendance_timestamp_is_plausible;

function attendance_date_for(p_event_ts in timestamp with time zone) return date is
begin
    if p_event_ts is null then
        return null;
    end if;
    return trunc(cast(p_event_ts at time zone c_attendance_timezone as date));
end attendance_date_for;

-- Same call shape as post_live/post_bulk/post_reconcile in production, so this
-- body depends on the procedure exactly like the production package body does.
procedure post_live_probe(p_body in clob) is
begin
    slic_zkt_recompute_daily_flags(p_body);
end post_live_probe;
end slic_zkt_truth_api;
/
