-- Test driver for the recompute proof (run_recompute_checks.py). RECOMP_LAB
-- holds the deterministic synthetic data and calls the OLD (exact production)
-- and NEW (migrated) procedures with identical requests. Every value comes
-- from DBMS_RANDOM with fixed seeds; nothing is read from outside this
-- disposable database.
begin
    execute immediate 'drop user recomp_lab cascade';
exception when others then if sqlcode <> -1918 then raise; end if;
end;
/
create user recomp_lab no authentication;
grant unlimited tablespace to recomp_lab;
grant select, insert, update on recomp_old.hr_raw_attn_capture_events to recomp_lab;
grant select, insert, update on recomp_new.hr_raw_attn_capture_events to recomp_lab;
grant execute on recomp_old.slic_zkt_recompute_daily_flags to recomp_lab;
grant execute on recomp_new.slic_zkt_recompute_daily_flags to recomp_lab;
grant execute on recomp_old.slic_zkt_truth_api to recomp_lab;
grant execute on recomp_old.recompute_write_counter to recomp_lab;
grant execute on recomp_new.recompute_write_counter to recomp_lab;
alter session set current_schema = recomp_lab;

create table seed_events as
select * from recomp_old.hr_raw_attn_capture_events where 1 = 0;
create table seed_index (idx number primary key, event_uid varchar2(600) not null);
create table employee_days (
    idx number primary key,
    cnic number not null,
    attendance_date date not null,
    row_count number not null
);
create table big_days (idx number primary key, cnic number not null, attendance_date date not null);
create table cnic_list (idx number primary key, cnic number not null);
create table timing_days (idx number primary key, cnic number not null, attendance_date date not null);
create table run_stats (name varchar2(40) primary key, value number not null);

create or replace package recompute_lab authid definer as
    procedure generate_seed;
    procedure load_seed;
    procedure run_equivalence(p_first_call in pls_integer, p_last_call in pls_integer);
    procedure report_equivalence(p_min_calls in pls_integer);
    procedure grow_tables(p_rows in pls_integer);
    procedure run_timing(
        p_old_calls in pls_integer,
        p_new_calls in pls_integer,
        p_min_speedup in number
    );
end recompute_lab;
/

create or replace package body recompute_lab as
    c_first_day constant date := date '2026-09-01';
    c_day_span constant pls_integer := 61;
    c_cnic_count constant pls_integer := 240;
    c_grow_first_day constant date := date '2024-12-01';
    c_grow_cnic_base constant number := 4100000000000;
    c_grow_id_base constant number := 10000000;
    c_iso constant varchar2(30) := 'YYYY-MM-DD"T"HH24:MI:SS';
    c_naive constant varchar2(30) := 'YYYY-MM-DD HH24:MI:SS';

    type t_counts is table of number index by varchar2(40);
    g_counts t_counts;
    g_day_count number;
    g_big_count number;
    g_cnic_total number;
    g_seed_total number;

    procedure bump(p_name in varchar2, p_by in number default 1) is
    begin
        if g_counts.exists(p_name) then
            g_counts(p_name) := g_counts(p_name) + p_by;
        else
            g_counts(p_name) := p_by;
        end if;
    end bump;

    function stat(p_name in varchar2) return number is
        l_value number;
    begin
        select value into l_value from run_stats where name = p_name;
        return l_value;
    exception
        when no_data_found then
            return 0;
    end stat;

    procedure add_stat(p_name in varchar2, p_value in number) is
    begin
        merge into run_stats s
        using (select p_name name, p_value value from dual) v
           on (s.name = v.name)
         when matched then update set s.value = s.value + v.value
         when not matched then insert (name, value) values (v.name, v.value);
    end add_stat;

    procedure persist_counts is
        l_name varchar2(40);
        l_value number;
    begin
        l_name := g_counts.first;
        while l_name is not null loop
            l_value := g_counts(l_name);
            add_stat(l_name, l_value);
            l_name := g_counts.next(l_name);
        end loop;
        g_counts.delete;
    end persist_counts;

    function seconds_between(
        p_start in timestamp with time zone,
        p_end in timestamp with time zone
    ) return number is
        l_diff interval day(9) to second(6) := p_end - p_start;
    begin
        return extract(day from l_diff) * 86400
             + extract(hour from l_diff) * 3600
             + extract(minute from l_diff) * 60
             + extract(second from l_diff);
    end seconds_between;

    procedure generate_seed is
        type t_rows is table of seed_events%rowtype;
        l_rows t_rows := t_rows();
        l_row seed_events%rowtype;
        l_id number := 1000;
        l_cnic number;
        l_day date;
        l_count pls_integer;
        l_r number;
        l_local timestamp(0);
        l_prev timestamp(0);
        l_ts timestamp(6) with time zone;
    begin
        delete from seed_events;
        delete from seed_index;
        delete from employee_days;
        delete from big_days;
        delete from cnic_list;
        delete from run_stats;
        dbms_random.seed(20261113);
        for c in 1 .. c_cnic_count loop
            -- A few short CNIC numbers let requests use leading zeros and
            -- surrounding spaces that still fit VARCHAR2(13).
            l_cnic := case when c <= 8 then 1000000 + c * 11 else 3520100000000 + c * 7919 end;
            insert into cnic_list (idx, cnic) values (c, l_cnic);
            for d in 0 .. c_day_span - 1 loop
                continue when dbms_random.value >= 0.55;
                l_day := c_first_day + d;
                l_r := dbms_random.value;
                l_count := case
                    when l_r < 0.45 then 1
                    when l_r < 0.75 then 2
                    when l_r < 0.88 then 3
                    when l_r < 0.97 then trunc(dbms_random.value(4, 9))
                    when l_r < 0.993 then trunc(dbms_random.value(9, 21))
                    else trunc(dbms_random.value(31, 41))
                end;
                l_prev := null;
                for k in 1 .. l_count loop
                    l_id := l_id + 1;
                    l_r := dbms_random.value;
                    if l_prev is not null and l_r < 0.08 then
                        l_local := l_prev; -- same instant: EVENT_UID breaks the tie
                    elsif l_r < 0.11 then
                        l_local := cast(l_day as timestamp);
                    elsif l_r < 0.13 then
                        l_local := cast(l_day as timestamp) + interval '0 23:59:59' day to second;
                    elsif l_r < 0.15 then
                        l_local := cast(l_day as timestamp) + interval '0 04:59:59' day to second;
                    else
                        l_local := cast(l_day as timestamp)
                            + numtodsinterval(trunc(dbms_random.value(0, 86400)), 'SECOND');
                    end if;
                    l_prev := l_local;
                    l_r := dbms_random.value;
                    -- Implausible times sit inside real employee-days so the
                    -- ranking must keep skipping them.
                    if l_r < 0.025 then
                        l_ts := from_tz(timestamp '2005-06-01 09:00:00', 'Asia/Karachi')
                            + numtodsinterval(k, 'MINUTE');
                    elsif l_r < 0.035 then
                        l_ts := from_tz(timestamp '2009-12-31 23:59:59', 'Asia/Karachi');
                    elsif l_r < 0.045 then
                        l_ts := from_tz(timestamp '2010-01-01 00:00:00', 'Asia/Karachi');
                    elsif l_r < 0.065 then
                        l_ts := systimestamp + numtodsinterval(3 + k, 'DAY');
                    elsif l_r < 0.075 then
                        l_ts := from_tz(timestamp '2099-01-01 08:00:00', 'Asia/Karachi');
                    elsif l_r < 0.70 then
                        l_ts := from_tz(l_local, 'Asia/Karachi');
                    elsif l_r < 0.85 then
                        l_ts := from_tz(l_local, '+05:00');
                    else
                        l_ts := from_tz(l_local, 'Asia/Karachi') at time zone 'UTC';
                    end if;
                    l_row.raw_capture_id := l_id;
                    l_row.event_uid := dbms_random.string('X', 10) || '-' || to_char(l_id);
                    l_row.zone_id := 'LAB-ZONE-' || mod(c, 7);
                    l_row.device_id := 'LAB-DEVICE-' || mod(c, 13);
                    l_row.device_serial := 'LAB-SERIAL';
                    l_row.user_id := to_char(c);
                    l_row.employee_name := 'Synthetic ' || c;
                    l_row.event_timestamp := l_ts;
                    l_row.clock_diff_seconds := 0;
                    l_row.capture_type := 'LIVE';
                    l_row.trust_status := 'TRUSTED_LIVE';
                    l_row.received_at := timestamp '2026-11-01 00:00:00 +05:00';
                    l_row.attendance_date := case when dbms_random.value < 0.01 then l_day + 1 else l_day end;
                    l_row.raw_punch := case when dbms_random.value < 0.10 then 'T' else 'F' end;
                    l_r := dbms_random.value;
                    l_row.check_in := case when l_r < 0.02 then null when l_r < 0.51 then 'T' else 'F' end;
                    l_r := dbms_random.value;
                    l_row.check_out := case when l_r < 0.02 then null when l_r < 0.51 then 'T' else 'F' end;
                    l_r := dbms_random.value;
                    l_row.datasync := case
                        when l_r < 0.45 then 0
                        when l_r < 0.90 then 1
                        when l_r < 0.95 then null
                        else 2
                    end;
                    l_row.source := 'LAB';
                    l_row.cnic := l_cnic;
                    l_rows.extend;
                    l_rows(l_rows.count) := l_row;
                end loop;
            end loop;
        end loop;
        forall i in 1 .. l_rows.count
            insert into seed_events values l_rows(i);

        insert into seed_index (idx, event_uid)
        select row_number() over (order by event_uid), event_uid from seed_events;
        insert into employee_days (idx, cnic, attendance_date, row_count)
        select row_number() over (order by cnic, attendance_date), cnic, attendance_date, count(*)
          from seed_events
         group by cnic, attendance_date;
        insert into big_days (idx, cnic, attendance_date)
        select row_number() over (order by cnic, attendance_date), cnic, attendance_date
          from employee_days
         where row_count >= 31;
        add_stat('next_id', 9000000);
        commit;
        select count(*) into g_seed_total from seed_events;
        select count(*) into g_day_count from employee_days;
        select count(*) into g_big_count from big_days;
        dbms_output.put_line(
            'recompute_seed rows=' || g_seed_total || ' employee_days=' || g_day_count
            || ' days_31_plus=' || g_big_count
        );
        if g_big_count = 0 then
            raise_application_error(-20090, 'Seed data has no employee-day with 31+ rows.');
        end if;
    end generate_seed;

    procedure load_seed is
    begin
        insert into recomp_old.hr_raw_attn_capture_events select * from seed_events;
        insert into recomp_new.hr_raw_attn_capture_events select * from seed_events;
        commit;
    end load_seed;

    procedure load_counts is
    begin
        select count(*) into g_day_count from employee_days;
        select count(*) into g_big_count from big_days;
        select count(*) into g_cnic_total from cnic_list;
        select count(*) into g_seed_total from seed_index;
    end load_counts;

    -- Random numbers are drawn in PL/SQL (exactly one draw per pick), never
    -- inside SQL where Oracle could evaluate them once per row.
    function random_index(p_count in number) return pls_integer is
    begin
        return trunc(dbms_random.value(1, p_count + 1));
    end random_index;

    function random_cnic return number is
        l_idx pls_integer := random_index(g_cnic_total);
        l_cnic number;
    begin
        select cnic into l_cnic from cnic_list where idx = l_idx;
        return l_cnic;
    end random_cnic;

    procedure pick_day(o_cnic out number, o_local out timestamp) is
        l_r number := dbms_random.value;
        l_day date;
        l_idx pls_integer;
    begin
        if l_r < 0.72 then
            l_idx := random_index(g_day_count);
            select cnic, attendance_date into o_cnic, l_day from employee_days
             where idx = l_idx;
            bump('day_existing');
        elsif l_r < 0.80 then
            l_idx := random_index(g_big_count);
            select cnic, attendance_date into o_cnic, l_day from big_days
             where idx = l_idx;
            bump('day_31_plus');
        elsif l_r < 0.90 then
            o_cnic := random_cnic;
            l_day := c_first_day + trunc(dbms_random.value(0, c_day_span));
            bump('day_random');
        elsif l_r < 0.95 then
            o_cnic := random_cnic;
            l_day := case trunc(dbms_random.value(1, 5))
                when 1 then date '2009-12-31'
                when 2 then date '2099-01-01'
                when 3 then trunc(sysdate) + 3
                else date '2010-01-01'
            end;
            bump('day_far');
        else
            o_cnic := 3529999990000 + trunc(dbms_random.value(0, 1000));
            l_day := c_first_day + trunc(dbms_random.value(0, c_day_span));
            bump('cnic_unknown');
        end if;
        l_r := dbms_random.value;
        o_local := cast(l_day as timestamp) + case
            when l_r < 0.04 then interval '0 00:00:00' day to second
            when l_r < 0.07 then interval '0 23:59:59' day to second
            when l_r < 0.10 then interval '0 04:59:59' day to second
            when l_r < 0.13 then interval '0 05:00:00' day to second
            else numtodsinterval(trunc(dbms_random.value(0, 86400)), 'SECOND')
        end;
    end pick_day;

    function cnic_json(p_cnic in number) return varchar2 is
        l_text varchar2(40) := to_char(p_cnic, 'FM9999999999999');
        l_r number := dbms_random.value;
    begin
        if l_r < 0.03 then
            bump('cnic_missing_or_blank');
            return case trunc(dbms_random.value(1, 7))
                when 1 then null -- key omitted
                when 2 then 'null'
                when 3 then '""'
                when 4 then '"   "'
                when 5 then '[]'
                else '{}'
            end;
        elsif l_r < 0.79 then
            return '"' || l_text || '"';
        elsif l_r < 0.87 then
            bump('cnic_json_number');
            return l_text;
        elsif l_r < 0.92 then
            bump('cnic_leading_zero');
            return '"' || lpad(l_text, 13, '0') || '"';
        elsif l_r < 0.97 then
            -- Trimmed when it still fits VARCHAR2(13), else NULL ON ERROR.
            bump('cnic_padded');
            return '" ' || l_text || ' "';
        end if;
        bump('cnic_too_long');
        return '"' || lpad(l_text, 14, '0') || '"';
    end cnic_json;

    function timestamp_json(p_local in timestamp) return varchar2 is
        l_utc timestamp := p_local - interval '5' hour;
        l_r number := dbms_random.value;
    begin
        if l_r < 0.36 then
            bump('ts_offset_pkt');
            return '"' || to_char(p_local, c_iso) || '+05:00"';
        elsif l_r < 0.56 then
            bump('ts_naive_pkt');
            return '"' || to_char(p_local, c_naive) || '"';
        elsif l_r < 0.72 then
            bump('ts_utc_z');
            return '"' || to_char(l_utc, c_iso) || 'Z"';
        elsif l_r < 0.75 then
            bump('ts_utc_lower_z');
            return '"' || to_char(l_utc, c_iso) || 'z"';
        elsif l_r < 0.78 then
            bump('ts_other_offset');
            return '"' || to_char(l_utc - interval '4' hour, c_iso) || '-04:00"';
        elsif l_r < 0.80 then
            bump('ts_other_offset');
            return '"' || to_char(l_utc + interval '570' minute(3), c_iso) || '+09:30"';
        elsif l_r < 0.82 then
            bump('ts_other_offset');
            return '"' || to_char(l_utc, c_iso) || '+00:00"';
        elsif l_r < 0.85 then
            bump('ts_padded');
            return '"  ' || to_char(p_local, c_naive) || ' "';
        end if;
        bump('ts_garbage');
        return case trunc(dbms_random.value(1, 16))
            when 1 then '"garbage"'
            when 2 then '""'
            when 3 then 'null'
            when 4 then null -- key omitted
            when 5 then '"2026-13-45T25:61:61+05:00"'
            when 6 then '"' || to_char(p_local, c_iso) || '"'
            when 7 then '"' || to_char(l_utc, c_iso) || '.123Z"'
            when 8 then '12345'
            when 9 then '"' || to_char(p_local, 'YYYY-MM-DD HH24:MI') || '"'
            when 10 then '"2026-02-30 10:00:00"'
            when 11 then '"' || to_char(p_local, c_iso) || '+05:00Z"'
            when 12 then '"' || to_char(p_local, c_iso) || '+0500"'
            when 13 then '"' || rpad(to_char(p_local, c_naive), 85, ' ') || 'x"'
            when 14 then '[1,2]'
            else '"' || to_char(p_local, 'YYYY-MM-DD') || 'T24:00:00+05:00"'
        end;
    end timestamp_json;

    function event_json(p_cnic_json in varchar2, p_ts_json in varchar2) return varchar2 is
        l_parts varchar2(4000);
        procedure put_field(p_key in varchar2, p_value in varchar2) is
        begin
            if p_value is not null then
                l_parts := l_parts || case when l_parts is not null then ',' end
                    || '"' || p_key || '":' || p_value;
            end if;
        end put_field;
    begin
        if dbms_random.value < 0.5 then
            put_field('cnic', p_cnic_json);
            put_field('timestamp', p_ts_json);
        else
            put_field('timestamp', p_ts_json);
            put_field('cnic', p_cnic_json);
        end if;
        put_field('raw_punch', case when dbms_random.value < 0.15 then '"T"' else '"F"' end);
        if dbms_random.value < 0.3 then
            put_field('event_uid', '"' || dbms_random.string('X', 12) || '"');
        end if;
        return '{' || l_parts || '}';
    end event_json;

    function next_body return clob is
        l_r number := dbms_random.value;
        l_events pls_integer;
        l_body clob;
        l_cnic number;
        l_local timestamp;
        l_prev_cnic number;
        l_prev_local timestamp;
        l_event varchar2(4000);
    begin
        if l_r < 0.05 then
            bump('special_bodies');
            pick_day(l_cnic, l_local);
            l_event := event_json(cnic_json(l_cnic), timestamp_json(l_local));
            return case trunc(dbms_random.value(1, 11))
                when 1 then null
                when 2 then '{}'
                when 3 then '{"events":[]}'
                when 4 then 'not json at all'
                when 5 then '{"events":[{"cnic":'
                when 6 then '{"events":' || l_event || '}'
                when 7 then '{"EVENTS":[' || l_event || ']}'
                when 8 then '{"events":[' || l_event || '],"extra":[' || l_event || ']}'
                when 9 then '{"events":[' || l_event || ',' || l_event || ']}'
                else '[' || l_event || ']'
            end;
        end if;
        l_r := dbms_random.value;
        l_events := case
            when l_r < 0.60 then 1
            when l_r < 0.85 then trunc(dbms_random.value(2, 5))
            when l_r < 0.95 then trunc(dbms_random.value(5, 11))
            when l_r < 0.99 then trunc(dbms_random.value(11, 21))
            else trunc(dbms_random.value(21, 41))
        end;
        for e in 1 .. l_events loop
            if e > 1 and dbms_random.value < 0.15 then
                -- The same employee-day again, usually written differently.
                l_cnic := l_prev_cnic;
                l_local := l_prev_local;
                bump('duplicate_day_events');
            else
                pick_day(l_cnic, l_local);
            end if;
            l_event := event_json(cnic_json(l_cnic), timestamp_json(l_local));
            l_body := l_body || case when e > 1 then ',' end || l_event;
            l_prev_cnic := l_cnic;
            l_prev_local := l_local;
        end loop;
        bump('events', l_events);
        if l_events > 1 then
            bump('multi_event_bodies');
        end if;
        return '{"events":[' || l_body || ']}';
    end next_body;

    -- What post_live does before it calls the procedure: insert the request's
    -- valid events with F/F flags (identical rows into both schemas).
    procedure insert_like_post_live(p_call in pls_integer, p_body in clob) is
        l_next number := stat('next_id');
        l_inserted number;
    begin
        insert into recomp_old.hr_raw_attn_capture_events (
            raw_capture_id, event_uid, zone_id, device_id, device_serial, user_id,
            employee_name, event_timestamp, clock_diff_seconds, capture_type,
            trust_status, received_at, attendance_date, check_in, check_out,
            raw_punch, datasync, source, cnic
        )
        select l_next + rownum,
               'L' || to_char(p_call) || '-' || to_char(rownum),
               'LAB-ZONE', 'LAB-DEVICE', 'LAB-SERIAL', 'LIVE', 'Synthetic Live',
               recomp_old.slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp),
               0, 'LIVE', 'TRUSTED_LIVE', systimestamp,
               recomp_old.slic_zkt_truth_api.attendance_date_for(
                   recomp_old.slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)
               ),
               'F', 'F', nvl(j.raw_punch, 'F'), 0, 'LAB', to_number(trim(j.cnic))
          from json_table(
                   p_body,
                   '$.events[*]'
                   columns
                       cnic varchar2(13) path '$.cnic' null on error,
                       event_timestamp varchar2(80) path '$.timestamp' null on error,
                       raw_punch varchar2(1) path '$.raw_punch' null on error
               ) j
         where regexp_like(trim(j.cnic), '^[0-9]{13}$')
           and recomp_old.slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) is not null;
        l_inserted := sql%rowcount;
        insert into recomp_new.hr_raw_attn_capture_events
        select *
          from recomp_old.hr_raw_attn_capture_events
         where raw_capture_id > l_next
           and raw_capture_id <= l_next + l_inserted;
        if sql%rowcount <> l_inserted then
            raise_application_error(-20091, 'post_live copy mismatch');
        end if;
        add_stat('next_id', l_inserted);
        bump('post_live_calls');
        bump('post_live_rows', l_inserted);
    end insert_like_post_live;

    -- Stale flags written identically into both schemas between calls.
    procedure dirty_rows(p_rows in pls_integer) is
        l_uid varchar2(600);
        l_in varchar2(4);
        l_out varchar2(4);
        l_sync number;
        l_idx pls_integer;
    begin
        for k in 1 .. p_rows loop
            l_idx := random_index(g_seed_total);
            select event_uid into l_uid from seed_index where idx = l_idx;
            l_in := case when dbms_random.value < 0.5 then 'T' else 'F' end;
            l_out := case when dbms_random.value < 0.5 then 'T' else 'F' end;
            l_sync := trunc(dbms_random.value(0, 3));
            update recomp_old.hr_raw_attn_capture_events
               set check_in = l_in, check_out = l_out, datasync = l_sync
             where event_uid = l_uid;
            update recomp_new.hr_raw_attn_capture_events
               set check_in = l_in, check_out = l_out, datasync = l_sync
             where event_uid = l_uid;
        end loop;
        bump('dirtied_rows', p_rows);
    end dirty_rows;

    function write_count(p_owner in varchar2) return number is
    begin
        return case p_owner
            when 'RECOMP_OLD' then recomp_old.recompute_write_counter.g_rows
            else recomp_new.recompute_write_counter.g_rows
        end;
    end write_count;

    procedure call_recompute(
        p_owner in varchar2,
        p_body in clob,
        o_code out number,
        o_writes out number,
        o_seconds out number
    ) is
        l_before number := write_count(p_owner);
        l_start timestamp with time zone := systimestamp;
    begin
        o_code := 0;
        begin
            execute immediate
                'begin ' || p_owner || '.slic_zkt_recompute_daily_flags(:body); end;'
                using p_body;
        exception
            when others then
                o_code := sqlcode;
        end;
        o_seconds := seconds_between(l_start, systimestamp);
        o_writes := write_count(p_owner) - l_before;
    end call_recompute;

    procedure fail(p_call in pls_integer, p_reason in varchar2, p_body in clob) is
    begin
        dbms_output.put_line('ORACLE_RECOMPUTE_EQUIVALENCE_FAILED call=' || p_call || ' ' || p_reason);
        dbms_output.put_line('body=' || dbms_lob.substr(p_body, 1500, 1));
        raise_application_error(
            -20092,
            substr(
                'ORACLE_RECOMPUTE_EQUIVALENCE_FAILED call=' || p_call || ' ' || p_reason
                || ' body=' || dbms_lob.substr(p_body, 600, 1),
                1,
                2000
            )
        );
    end fail;

    procedure assert_identical(p_call in pls_integer, p_body in clob) is
        l_old_only number;
        l_new_only number;
        l_old_rows number;
        l_new_rows number;
        l_sample varchar2(1500);
    begin
        select count(*) into l_old_only from (
            select event_uid, check_in, check_out, datasync from recomp_old.hr_raw_attn_capture_events
            minus
            select event_uid, check_in, check_out, datasync from recomp_new.hr_raw_attn_capture_events
        );
        select count(*) into l_new_only from (
            select event_uid, check_in, check_out, datasync from recomp_new.hr_raw_attn_capture_events
            minus
            select event_uid, check_in, check_out, datasync from recomp_old.hr_raw_attn_capture_events
        );
        select count(*) into l_old_rows from recomp_old.hr_raw_attn_capture_events;
        select count(*) into l_new_rows from recomp_new.hr_raw_attn_capture_events;
        if l_old_only + l_new_only > 0 or l_old_rows <> l_new_rows then
            for diff in (
                select * from (
                    select 'OLD' side, event_uid, check_in, check_out, datasync from (
                        select event_uid, check_in, check_out, datasync from recomp_old.hr_raw_attn_capture_events
                        minus
                        select event_uid, check_in, check_out, datasync from recomp_new.hr_raw_attn_capture_events
                    )
                    union all
                    select 'NEW' side, event_uid, check_in, check_out, datasync from (
                        select event_uid, check_in, check_out, datasync from recomp_new.hr_raw_attn_capture_events
                        minus
                        select event_uid, check_in, check_out, datasync from recomp_old.hr_raw_attn_capture_events
                    )
                    order by 2, 1
                ) where rownum <= 8
            ) loop
                l_sample := substr(
                    l_sample || diff.side || ' ' || diff.event_uid || ' in=' || diff.check_in
                    || ' out=' || diff.check_out || ' sync=' || diff.datasync || '; ',
                    1,
                    1500
                );
            end loop;
            fail(
                p_call,
                'old_only=' || l_old_only || ' new_only=' || l_new_only || ' old_rows='
                || l_old_rows || ' new_rows=' || l_new_rows || ' sample: ' || l_sample,
                p_body
            );
        end if;
    end assert_identical;

    procedure run_equivalence(p_first_call in pls_integer, p_last_call in pls_integer) is
        l_body clob;
        l_old_code number;
        l_new_code number;
        l_old_writes number;
        l_new_writes number;
        l_old_seconds number;
        l_new_seconds number;
        l_old_total number := 0;
        l_new_total number := 0;
        l_writes number := 0;
    begin
        g_counts.delete;
        load_counts;
        for l_call in p_first_call .. p_last_call loop
            dbms_random.seed(20261113 + l_call);
            if mod(l_call, 8) = 0 then
                dirty_rows(40);
            end if;
            l_body := next_body;
            if dbms_random.value < 0.35 then
                insert_like_post_live(l_call, l_body);
            end if;
            commit;

            call_recompute('RECOMP_OLD', l_body, l_old_code, l_old_writes, l_old_seconds);
            call_recompute('RECOMP_NEW', l_body, l_new_code, l_new_writes, l_new_seconds);
            if l_old_code <> l_new_code then
                fail(l_call, 'old_sqlcode=' || l_old_code || ' new_sqlcode=' || l_new_code, l_body);
            end if;
            if l_old_writes <> l_new_writes then
                fail(l_call, 'old_row_writes=' || l_old_writes || ' new_row_writes=' || l_new_writes, l_body);
            end if;
            assert_identical(l_call, l_body);
            commit;

            bump('calls');
            if l_old_code <> 0 then
                bump('calls_both_raised');
            end if;
            bump('row_writes', l_old_writes);
            if l_old_writes > 0 then
                bump('calls_writing_rows');
            end if;
            l_writes := l_writes + l_old_writes;
            l_old_total := l_old_total + l_old_seconds;
            l_new_total := l_new_total + l_new_seconds;
        end loop;
        bump('old_ms', round(l_old_total * 1000));
        bump('new_ms', round(l_new_total * 1000));
        persist_counts;
        commit;
        dbms_output.put_line(
            'recompute_equivalence calls ' || p_first_call || '-' || p_last_call
            || ': identical after every call; row_writes=' || l_writes
            || ' old_s=' || round(l_old_total, 2) || ' new_s=' || round(l_new_total, 2)
        );
    end run_equivalence;

    procedure report_equivalence(p_min_calls in pls_integer) is
        l_rows number;
        l_days number;
        l_big number;
        l_line varchar2(4000);
    begin
        for required in (
            select column_value name
              from table(sys.odcivarchar2list(
                       'calls', 'events', 'multi_event_bodies', 'special_bodies',
                       'post_live_calls', 'post_live_rows', 'dirtied_rows',
                       'row_writes', 'calls_writing_rows', 'duplicate_day_events',
                       'day_existing', 'day_31_plus', 'day_random', 'day_far',
                       'cnic_unknown', 'cnic_json_number', 'cnic_leading_zero',
                       'cnic_padded', 'cnic_too_long', 'cnic_missing_or_blank',
                       'ts_offset_pkt', 'ts_naive_pkt', 'ts_utc_z', 'ts_utc_lower_z',
                       'ts_other_offset', 'ts_padded', 'ts_garbage'))
        ) loop
            if stat(required.name) = 0 then
                raise_application_error(-20093, 'Equivalence coverage missing: ' || required.name);
            end if;
        end loop;
        if stat('calls') < p_min_calls then
            raise_application_error(-20094, 'Only ' || stat('calls') || ' equivalence calls ran.');
        end if;
        select count(*) into l_rows from recomp_old.hr_raw_attn_capture_events;
        select count(*) into l_days from (
            select distinct cnic, attendance_date from recomp_old.hr_raw_attn_capture_events
        );
        select count(*) into l_big from (
            select cnic, attendance_date from recomp_old.hr_raw_attn_capture_events
             group by cnic, attendance_date having count(*) >= 31
        );
        for r in (select name, value from run_stats where name <> 'next_id' order by name) loop
            l_line := l_line || ' ' || r.name || '=' || r.value;
        end loop;
        dbms_output.put_line('recompute_equivalence_counts' || l_line);
        dbms_output.put_line(
            'ORACLE_RECOMPUTE_EQUIVALENCE_PASSED calls=' || stat('calls')
            || ' events=' || stat('events') || ' special_bodies=' || stat('special_bodies')
            || ' post_live_calls=' || stat('post_live_calls')
            || ' post_live_rows=' || stat('post_live_rows')
            || ' row_writes=' || stat('row_writes')
            || ' calls_writing_rows=' || stat('calls_writing_rows')
            || ' table_rows=' || l_rows || ' employee_days=' || l_days
            || ' days_31_plus=' || l_big
            || ' old_s=' || round(stat('old_ms') / 1000, 1)
            || ' new_s=' || round(stat('new_ms') / 1000, 1)
        );
    end report_equivalence;

    procedure grow_tables(p_rows in pls_integer) is
        l_old number;
        l_new number;
    begin
        -- About 2 rows per employee-day, like production; stale flags.
        insert /*+ append */ into recomp_old.hr_raw_attn_capture_events (
            raw_capture_id, event_uid, zone_id, device_id, device_serial, user_id,
            employee_name, event_timestamp, clock_diff_seconds, capture_type,
            trust_status, received_at, attendance_date, check_in, check_out,
            raw_punch, datasync, source, cnic
        )
        select c_grow_id_base + g.n,
               'G' || to_char(g.n, 'FM00000000'),
               'GROW-ZONE', 'GROW-DEVICE', 'GROW-SERIAL', to_char(mod(g.n, 600)),
               'Synthetic Growth',
               from_tz(
                   cast(c_grow_first_day + trunc(g.n / 1200) as timestamp)
                   + numtodsinterval(mod(ora_hash(g.n), 86400), 'SECOND'),
                   'Asia/Karachi'
               ),
               0, 'LIVE', 'TRUSTED_LIVE', timestamp '2026-11-01 00:00:00 +05:00',
               c_grow_first_day + trunc(g.n / 1200),
               case when mod(ora_hash(g.n, 1000, 7), 2) = 0 then 'T' else 'F' end,
               case when mod(ora_hash(g.n, 1000, 11), 3) = 0 then 'T' else 'F' end,
               case when mod(ora_hash(g.n, 1000, 3), 10) = 0 then 'T' else 'F' end,
               mod(g.n, 3), 'GROW', c_grow_cnic_base + mod(g.n, 600)
          from (select level n from dual connect by level <= p_rows) g;
        commit;
        insert /*+ append */ into recomp_new.hr_raw_attn_capture_events
        select * from recomp_old.hr_raw_attn_capture_events
         where raw_capture_id > c_grow_id_base;
        commit;
        select count(*) into l_old from recomp_old.hr_raw_attn_capture_events;
        select count(*) into l_new from recomp_new.hr_raw_attn_capture_events;
        dbms_output.put_line('recompute_timing_tables rows_old=' || l_old || ' rows_new=' || l_new);
        if l_old <> l_new then
            raise_application_error(-20095, 'Grown tables differ in size.');
        end if;
    end grow_tables;

    function timing_body(p_idx in pls_integer) return clob is
        l_cnic number;
        l_day date;
    begin
        select cnic, attendance_date into l_cnic, l_day from timing_days where idx = p_idx;
        return '{"events":[{"cnic":"' || to_char(l_cnic, 'FM9999999999999')
            || '","timestamp":"' || to_char(l_day, 'YYYY-MM-DD') || 'T10:15:00+05:00"}]}';
    end timing_body;

    procedure run_timing(
        p_old_calls in pls_integer,
        p_new_calls in pls_integer,
        p_min_speedup in number
    ) is
        l_body clob;
        l_code number;
        l_writes number;
        l_seconds number;
        l_cpu number;
        l_old_seconds number := 0;
        l_new_seconds number := 0;
        l_old_cpu number := 0;
        l_new_cpu number := 0;
        l_mismatch number;
        l_rows number;
        l_rows_t number;
        l_in number;
        l_out number;
        l_first varchar2(600);
        l_last varchar2(600);
        l_first_in varchar2(4);
        l_last_out varchar2(4);
        l_checked number := 0;
        l_old_avg number;
        l_new_avg number;
        l_speedup number;
    begin
        delete from timing_days;
        insert into timing_days (idx, cnic, attendance_date)
        select level, c_grow_cnic_base + mod(level * 37, 600), c_grow_first_day + 7 + level * 10
          from dual connect by level <= p_new_calls;
        commit;
        select count(*) into l_rows from recomp_old.hr_raw_attn_capture_events;

        for k in 1 .. p_old_calls loop
            l_body := timing_body(k);
            l_cpu := dbms_utility.get_cpu_time;
            call_recompute('RECOMP_OLD', l_body, l_code, l_writes, l_seconds);
            l_old_cpu := l_old_cpu + (dbms_utility.get_cpu_time - l_cpu) / 100;
            if l_code <> 0 then
                raise_application_error(-20096, 'OLD timing call failed: ' || l_code);
            end if;
            l_old_seconds := l_old_seconds + l_seconds;
            commit;
            dbms_output.put_line('recompute_timing OLD call ' || k || ': ' || round(l_seconds, 3) || ' s');
        end loop;
        for k in 1 .. p_new_calls loop
            l_body := timing_body(k);
            l_cpu := dbms_utility.get_cpu_time;
            call_recompute('RECOMP_NEW', l_body, l_code, l_writes, l_seconds);
            l_new_cpu := l_new_cpu + (dbms_utility.get_cpu_time - l_cpu) / 100;
            if l_code <> 0 then
                raise_application_error(-20097, 'NEW timing call failed: ' || l_code);
            end if;
            l_new_seconds := l_new_seconds + l_seconds;
            commit;
        end loop;

        -- Same rows wherever both versions ran (everything except the
        -- employee-days only NEW recomputed).
        select count(*) into l_mismatch from (
            (select o.event_uid, o.check_in, o.check_out, o.datasync
               from recomp_old.hr_raw_attn_capture_events o
              where not exists (
                        select 1 from timing_days t
                         where t.idx > p_old_calls and t.cnic = o.cnic
                           and t.attendance_date = o.attendance_date)
             minus
             select n.event_uid, n.check_in, n.check_out, n.datasync
               from recomp_new.hr_raw_attn_capture_events n
              where not exists (
                        select 1 from timing_days t
                         where t.idx > p_old_calls and t.cnic = n.cnic
                           and t.attendance_date = n.attendance_date))
            union all
            (select n.event_uid, n.check_in, n.check_out, n.datasync
               from recomp_new.hr_raw_attn_capture_events n
              where not exists (
                        select 1 from timing_days t
                         where t.idx > p_old_calls and t.cnic = n.cnic
                           and t.attendance_date = n.attendance_date)
             minus
             select o.event_uid, o.check_in, o.check_out, o.datasync
               from recomp_old.hr_raw_attn_capture_events o
              where not exists (
                        select 1 from timing_days t
                         where t.idx > p_old_calls and t.cnic = o.cnic
                           and t.attendance_date = o.attendance_date))
        );
        if l_mismatch <> 0 then
            raise_application_error(-20098, 'After timing, OLD and NEW differ in ' || l_mismatch || ' rows.');
        end if;

        -- Independent check of every NEW-only day: exactly one check-in on the
        -- first normal punch and one check-out on the last when 2+ exist.
        for t in (select idx, cnic, attendance_date from timing_days order by idx) loop
            select count(*),
                   count(case when check_in = 'T' then 1 end),
                   count(case when check_out = 'T' then 1 end),
                   min(event_uid) keep (dense_rank first order by event_timestamp, event_uid),
                   max(event_uid) keep (dense_rank last order by event_timestamp, event_uid)
              into l_rows_t, l_in, l_out, l_first, l_last
              from recomp_new.hr_raw_attn_capture_events
             where cnic = t.cnic
               and attendance_date = t.attendance_date
               and raw_punch = 'F'
               and event_timestamp >= timestamp '2010-01-01 00:00:00 Asia/Karachi'
               and event_timestamp <= systimestamp + interval '1' day;
            if l_rows_t > 0 then
                select check_in into l_first_in from recomp_new.hr_raw_attn_capture_events
                 where event_uid = l_first;
                select check_out into l_last_out from recomp_new.hr_raw_attn_capture_events
                 where event_uid = l_last;
                if l_in <> 1 or l_first_in <> 'T'
                   or l_out <> case when l_rows_t > 1 then 1 else 0 end
                   or (l_rows_t > 1 and l_last_out <> 'T') then
                    raise_application_error(
                        -20099,
                        'NEW flags wrong for cnic ' || t.cnic || ' day '
                        || to_char(t.attendance_date, 'YYYY-MM-DD')
                    );
                end if;
                l_checked := l_checked + 1;
            end if;
        end loop;

        l_old_avg := l_old_seconds / p_old_calls;
        l_new_avg := l_new_seconds / p_new_calls;
        l_speedup := l_old_avg / greatest(l_new_avg, 0.000001);
        dbms_output.put_line(
            'recompute_timing rows=' || l_rows
            || ' old_calls=' || p_old_calls || ' old_avg_ms=' || round(l_old_avg * 1000, 1)
            || ' old_cpu_avg_ms=' || round(l_old_cpu / p_old_calls * 1000, 1)
            || ' new_calls=' || p_new_calls || ' new_avg_ms=' || round(l_new_avg * 1000, 3)
            || ' new_cpu_avg_ms=' || round(l_new_cpu / p_new_calls * 1000, 3)
            || ' speedup=' || round(l_speedup) || 'x'
            || ' new_days_verified=' || l_checked
        );
        if l_speedup < p_min_speedup then
            raise_application_error(
                -20100,
                'ORACLE_RECOMPUTE_TIMING_FAILED speedup ' || round(l_speedup, 1)
                || 'x is below ' || p_min_speedup || 'x'
            );
        end if;
        if l_checked < p_new_calls / 2 then
            raise_application_error(-20101, 'Too few timing days had normal punches to verify.');
        end if;
        dbms_output.put_line(
            'ORACLE_RECOMPUTE_TIMING_PASSED rows=' || l_rows
            || ' old_avg_ms=' || round(l_old_avg * 1000, 1)
            || ' new_avg_ms=' || round(l_new_avg * 1000, 3)
            || ' speedup=' || round(l_speedup) || 'x'
            || ' old_calls=' || p_old_calls || ' new_calls=' || p_new_calls
        );
    end run_timing;
end recompute_lab;
/
alter session set current_schema = sys;
