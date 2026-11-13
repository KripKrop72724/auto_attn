-- One disposable schema for the recompute proof (run_recompute_checks.py).
-- The runner fills in the schema name (RECOMP_OLD, RECOMP_NEW, ...). Every
-- object mirrors production SLIC_HRM: the raw table with only its primary key
-- and EVENT_UID unique key, the three truth API helpers, the production
-- procedure, and the two package bodies that call it.
begin
    execute immediate 'drop user __SCHEMA__ cascade';
exception when others then if sqlcode <> -1918 then raise; end if;
end;
/
create user __SCHEMA__ no authentication;
grant unlimited tablespace to __SCHEMA__;
alter session set current_schema = __SCHEMA__;

-- Production column list and order. CHECK_IN/CHECK_OUT are nullable here so
-- the proof also covers NULL flags (a superset of production states).
create table hr_raw_attn_capture_events (
    raw_capture_id number not null,
    event_uid varchar2(600) not null,
    zone_id varchar2(200) not null,
    device_id varchar2(400) not null,
    device_serial varchar2(400),
    user_id varchar2(200) not null,
    employee_name varchar2(800),
    event_timestamp timestamp(6) with time zone not null,
    clock_diff_seconds number(10,3),
    capture_type varchar2(120) not null,
    trust_status varchar2(240) not null,
    received_at timestamp(6) with time zone not null,
    attendance_date date not null,
    check_in varchar2(4),
    check_out varchar2(4),
    raw_punch varchar2(4) not null,
    datasync number,
    source varchar2(100),
    cnic number not null,
    constraint pk_raw primary key (raw_capture_id),
    constraint uk_hr_raw_attn_event_uid unique (event_uid)
);

create or replace package slic_zkt_truth_api authid definer as
    function parse_event_timestamp(p_value in varchar2) return timestamp with time zone;
    function attendance_timestamp_is_plausible(
        p_event_ts in timestamp with time zone
    ) return number;
    function attendance_date_for(p_event_ts in timestamp with time zone) return date;
    procedure post_live_probe(p_body in clob);
end slic_zkt_truth_api;
/

-- __PRODUCTION_PROCEDURE__

-- __TRUTH_API_BODY__

create or replace package slic_zkt_identity_repair_api authid definer as
    procedure recompute_probe(p_body in clob);
end slic_zkt_identity_repair_api;
/
create or replace package body slic_zkt_identity_repair_api as
    procedure recompute_probe(p_body in clob) is
    begin
        slic_zkt_recompute_daily_flags(p_body);
    end recompute_probe;
end slic_zkt_identity_repair_api;
/

-- Test-only instrumentation, dropped before timing: counts every row the
-- UPDATE and MERGE write, so OLD and NEW must also write the same rows.
create or replace package recompute_write_counter as
    g_rows number := 0;
end recompute_write_counter;
/
create or replace trigger hr_raw_attn_count_writes
after update on hr_raw_attn_capture_events
for each row
begin
    recompute_write_counter.g_rows := recompute_write_counter.g_rows + 1;
end;
/
alter session set current_schema = sys;
