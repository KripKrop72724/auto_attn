set define off
set serveroutput on
whenever sqlerror exit failure rollback

-- Reviewed source only: do not run against production as part of ADD deployment.
-- This independent reader contains no attendance DML, transaction control,
-- dynamic SQL or calls to a repair package. Installation changes package metadata.
-- Install/authenticate its ORDS route separately after isolated Oracle qualification.
create or replace package slic_zkt_delivery_v2 authid definer as
    function inspect_projection(p_body in clob) return clob;
    procedure post_check(p_body in clob);
end slic_zkt_delivery_v2;
/
create or replace package body slic_zkt_delivery_v2 as
    c_scope constant varchar2(80) := 'ORACLE_RAW_DAY_TIMES_V2';
    c_username constant varchar2(128) := 'REPLACE_WITH_ADD_API_USERNAME';
    c_password_sha256 constant varchar2(64) := 'REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX';

    function digest(p_value in varchar2) return varchar2 is
        l_hash varchar2(64);
    begin
        select lower(rawtohex(standard_hash(p_value, 'SHA256'))) into l_hash from dual;
        return l_hash;
    end;

    function part(p_value in varchar2) return varchar2 is
    begin
        if p_value is null then return '-1:'; end if;
        return to_char(lengthb(p_value), 'FM99999') || ':' || p_value;
    end;

    function number_text(p_value in number) return varchar2 is
    begin
        return to_char(p_value, 'TM9', 'NLS_NUMERIC_CHARACTERS=''.,''');
    end;

    function instant(p_value in timestamp with time zone) return varchar2 is
    begin
        return to_char(p_value at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"');
    end;

    function local_time(p_value in date) return varchar2 is
    begin
        return to_char(p_value, 'YYYY-MM-DD"T"HH24:MI:SS');
    end;

    function inspect_projection(p_body in clob) return clob is
        l_body json_object_t;
        l_projection json_object_t;
        l_result json_object_t := json_object_t();
        l_response json_object_t := json_object_t();
        l_results json_array_t := json_array_t();
        l_request varchar2(64);
        l_uid varchar2(64);
        l_zone varchar2(200);
        l_device varchar2(400);
        l_serial varchar2(400);
        l_user varchar2(200);
        l_name varchar2(800);
        l_cnic varchar2(13);
        l_timestamp varchar2(40);
        l_ts timestamp with time zone;
        l_day date;
        l_raw varchar2(1);
        l_capture varchar2(120);
        l_trust varchar2(240);
        l_clock varchar2(32);
        l_clock_number number(10,3);
        l_stored hr_raw_attn_capture_events%rowtype;
        l_target_count number;
        l_people number;
        l_employee number;
        l_normal_count number;
        l_first_uid varchar2(600);
        l_last_uid varchar2(600);
        l_first date;
        l_last date;
        l_invalid number;
        l_daily_count number;
        l_daily_first date;
        l_daily_last date;
        l_protected number;
        l_class varchar2(40);
        l_downstream varchar2(40) := 'NOT_VERIFIED';
        l_raw_match boolean := false;
        l_expected_in varchar2(1);
        l_expected_out varchar2(1);
        l_token varchar2(64);
        l_material varchar2(32767);
        function required_text(p_key in varchar2, p_max in pls_integer) return varchar2 is
            l_value varchar2(32767);
            l_element json_element_t;
        begin
            l_element := l_projection.get(p_key);
            if l_element is null or not l_element.is_string then
                raise_application_error(-20670, 'INVALID_PROJECTION');
            end if;
            l_value := l_projection.get_string(p_key);
            if l_value is null or lengthb(l_value) > p_max or instr(l_value, chr(0)) > 0 then
                raise_application_error(-20670, 'INVALID_PROJECTION');
            end if;
            return l_value;
        end;
    begin
        if p_body is null or dbms_lob.getlength(p_body) > 12000 then
            raise_application_error(-20670, 'INVALID_PROJECTION');
        end if;
        l_body := json_object_t.parse(p_body);
        if nvl(l_body.get_string('contract_version'), '?') <> '2'
           or nvl(l_body.get_string('verification_scope'), '?') <> c_scope then
            raise_application_error(-20670, 'CONTRACT_UNSUPPORTED');
        end if;
        l_request := l_body.get_string('request_digest');
        if l_request is null or not regexp_like(l_request, '^[0-9a-f]{64}$', 'c') then
            raise_application_error(-20670, 'INVALID_REQUEST_DIGEST');
        end if;
        l_projection := l_body.get_object('projection');
        if l_projection is null then raise_application_error(-20670, 'INVALID_PROJECTION'); end if;
        l_uid := required_text('event_uid', 64);
        l_zone := required_text('zone_id', 200);
        l_device := required_text('device_id', 400);
        l_serial := required_text('device_serial', 400);
        l_user := required_text('user_id', 200);
        l_name := required_text('employee_name', 800);
        l_cnic := required_text('cnic', 13);
        l_timestamp := required_text('timestamp', 27);
        l_raw := required_text('raw_punch', 1);
        l_capture := required_text('capturetype', 120);
        l_trust := required_text('trust_status', 240);
        if not l_projection.has('clockdiff') then raise_application_error(-20670, 'INVALID_PROJECTION'); end if;
        if not l_projection.get('clockdiff').is_null then l_clock := required_text('clockdiff', 32); end if;
        if not regexp_like(l_uid, '^[0-9a-f]{64}$', 'c')
           or not regexp_like(l_cnic, '^[1-9][0-9]{12}$', 'c')
           or not regexp_like(l_timestamp, '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$', 'c')
           or l_raw not in ('T','F')
           or (l_clock is not null and not regexp_like(l_clock, '^-?[0-9]{1,7}[.][0-9]{3}$', 'c')) then
            raise_application_error(-20670, 'INVALID_PROJECTION');
        end if;
        l_ts := from_tz(to_timestamp(substr(l_timestamp, 1, 26),
                                    'YYYY-MM-DD"T"HH24:MI:SS.FF6'), 'UTC');
        if instant(l_ts) <> l_timestamp then raise_application_error(-20670, 'INVALID_TIMESTAMP'); end if;
        l_clock_number := to_number(l_clock, '9999999D999', 'NLS_NUMERIC_CHARACTERS=''.,''');
        l_day := trunc(cast(l_ts at time zone 'Asia/Karachi' as date));

        -- All observed raw/identity/day values come from ONE statement snapshot.
        -- Only aggregates leave a day scan; its output does not grow with traffic.
        begin
            with people as (
                select count(*) amount, min(employee_id) employee_id
                  from hr_employee where cnic = to_number(l_cnic)
            ), normal as (
                select event_uid, event_timestamp,
                       row_number() over (order by event_timestamp, event_uid) first_rank,
                       row_number() over (order by event_timestamp desc, event_uid desc) last_rank,
                       case when trust_status = 'SUSPECT_DEVICE_TIME'
                              or trunc(cast(event_timestamp at time zone 'Asia/Karachi' as date)) <> attendance_date
                            then 1 else 0 end invalid
                  from hr_raw_attn_capture_events
                 where cnic = to_number(l_cnic) and attendance_date = l_day and raw_punch = 'F'
            ), totals as (
                select count(*) amount,
                       max(case when first_rank = 1 then event_uid end) first_uid,
                       max(case when last_rank = 1 then event_uid end) last_uid,
                       min(cast(event_timestamp at time zone 'Asia/Karachi' as date)) first_time,
                       max(cast(event_timestamp at time zone 'Asia/Karachi' as date)) last_time,
                       nvl(sum(invalid), 0) invalid
                  from normal
            ), day_row as (
                select count(*) amount, min(check_in_time) first_time, max(check_out_time) last_time,
                       nvl(max(case when nvl(marked_by, '?') <> 'BIOMETRIC'
                                      or leave_application_id is not null or od_request_id is not null
                                    then 1 else 0 end), 0) protected
                  from hr_employee_attendance
                 where employee_id = (select employee_id from people) and attendance_date = l_day
            )
            select d.zone_id, d.device_id, d.device_serial, d.user_id, d.employee_name,
                   d.cnic, d.event_timestamp, d.raw_punch, d.capture_type, d.trust_status,
                   d.clock_diff_seconds, d.attendance_date, d.received_at, d.check_in, d.check_out, d.datasync,
                   count(*) over (), p.amount, p.employee_id,
                   n.amount, n.first_uid, n.last_uid, n.first_time, n.last_time, n.invalid,
                   a.amount, a.first_time, a.last_time, a.protected
              into l_stored.zone_id, l_stored.device_id, l_stored.device_serial, l_stored.user_id, l_stored.employee_name,
                   l_stored.cnic, l_stored.event_timestamp, l_stored.raw_punch, l_stored.capture_type, l_stored.trust_status,
                   l_stored.clock_diff_seconds, l_stored.attendance_date, l_stored.received_at,
                   l_stored.check_in, l_stored.check_out, l_stored.datasync,
                   l_target_count, l_people, l_employee,
                   l_normal_count, l_first_uid, l_last_uid, l_first, l_last, l_invalid,
                   l_daily_count, l_daily_first, l_daily_last, l_protected
              from hr_raw_attn_capture_events d cross join people p cross join totals n cross join day_row a
             where d.event_uid = l_uid;
        exception
            when no_data_found then l_class := 'MISSING';
            when too_many_rows then l_class := 'CROSS_DEVICE_UID_COLLISION';
        end;
        if l_class is null then
            l_raw_match := l_target_count = 1
                and nvl(l_stored.zone_id, chr(0)) = l_zone
                and nvl(l_stored.device_id, chr(0)) = l_device
                and nvl(l_stored.device_serial, chr(0)) = l_serial
                and nvl(l_stored.user_id, chr(0)) = l_user
                and nvl(l_stored.employee_name, chr(0)) = l_name
                and l_stored.cnic = to_number(l_cnic)
                and l_stored.event_timestamp = l_ts
                and nvl(l_stored.raw_punch, '?') = l_raw
                and nvl(l_stored.capture_type, chr(0)) = l_capture
                and nvl(l_stored.trust_status, chr(0)) = l_trust
                and ((l_stored.clock_diff_seconds is null and l_clock_number is null)
                     or l_stored.clock_diff_seconds = l_clock_number)
                and l_stored.attendance_date = l_day
                and l_stored.received_at is not null;
            if not l_raw_match or l_raw_match is null then
                l_class := 'MISMATCH';
            elsif l_raw = 'T' then
                if l_stored.check_in = 'F' and l_stored.check_out = 'F' then
                    l_class := 'MATCH'; l_downstream := 'RAW_ONLY';
                else l_class := 'DOWNSTREAM_PENDING'; end if;
            elsif l_people <> 1 then
                l_class := 'IDENTITY_HOLD';
            elsif l_invalid <> 0 or l_protected <> 0 or l_daily_count > 1 then
                l_class := 'DOWNSTREAM_HOLD';
            else
                l_expected_in := case when l_uid = l_first_uid then 'T' else 'F' end;
                l_expected_out := case when l_normal_count > 1 and l_uid = l_last_uid then 'T' else 'F' end;
                if l_daily_count = 1 and l_normal_count > 0
                   and l_stored.check_in = l_expected_in and l_stored.check_out = l_expected_out
                   and l_stored.datasync = 1 and l_daily_first = l_first
                   and ((l_normal_count = 1 and l_daily_last is null)
                        or (l_normal_count > 1 and l_daily_last = l_last)) then
                    l_class := 'MATCH'; l_downstream := 'MATCH';
                else l_class := 'DOWNSTREAM_PENDING'; end if;
            end if;
        end if;
        l_material := part(c_scope) || part(l_request) || part(l_uid) || part(l_class)
            || part(l_stored.zone_id) || part(l_stored.device_id) || part(l_stored.device_serial)
            || part(l_stored.user_id) || part(l_stored.employee_name) || part(number_text(l_stored.cnic))
            || part(instant(l_stored.event_timestamp)) || part(l_stored.raw_punch)
            || part(l_stored.capture_type) || part(l_stored.trust_status)
            || part(number_text(l_stored.clock_diff_seconds)) || part(local_time(l_stored.attendance_date))
            || part(instant(l_stored.received_at)) || part(l_stored.check_in) || part(l_stored.check_out)
            || part(number_text(l_stored.datasync)) || part(number_text(l_people)) || part(number_text(l_employee))
            || part(number_text(l_normal_count)) || part(l_first_uid) || part(l_last_uid)
            || part(local_time(l_first)) || part(local_time(l_last)) || part(number_text(l_invalid))
            || part(number_text(l_daily_count)) || part(local_time(l_daily_first)) || part(local_time(l_daily_last))
            || part(number_text(l_protected)) || part(l_downstream);
        l_token := digest(l_material);
        l_result.put('event_uid', l_uid);
        l_result.put('classification', l_class);
        l_result.put('current_content_token', l_token);
        if l_raw_match then l_result.put('raw_projection_verified', true);
        else l_result.put('raw_projection_verified', false); end if;
        l_result.put('downstream_status', l_downstream);
        l_results.append(l_result);
        l_response.put('success', true);
        l_response.put('contract_version', '2');
        l_response.put('verification_scope', c_scope);
        l_response.put('request_digest', l_request);
        l_response.put('results', l_results);
        return l_response.to_clob;
    end;

    procedure post_check(p_body in clob) is
        l_username varchar2(512);
        l_password varchar2(1024);
        l_json clob;
        l_status number := 200;
    begin
        l_username := coalesce(owa_util.get_cgi_env('HTTP_X_API_USERNAME'), owa_util.get_cgi_env('X_API_USERNAME'));
        l_password := coalesce(owa_util.get_cgi_env('HTTP_X_API_PASSWORD'), owa_util.get_cgi_env('X_API_PASSWORD'));
        if nvl(l_username, chr(0)) <> c_username or digest(nvl(l_password, chr(0))) <> c_password_sha256 then
            l_status := 401; l_json := '{"success":false,"error_code":"ADD_ONLY_AUTH_REQUIRED"}';
        else
            begin l_json := inspect_projection(p_body);
            exception when others then
                l_status := 400; l_json := '{"success":false,"error_code":"PROJECTION_CHECK_FAILED"}';
            end;
        end if;
        owa_util.status_line(l_status, null, false);
        owa_util.mime_header('application/json', false);
        owa_util.http_header_close;
        htp.prn(dbms_lob.substr(l_json, 32767, 1));
    end;
end slic_zkt_delivery_v2;
/
show errors package body slic_zkt_delivery_v2

declare
    l_valid number;
    l_errors number;
begin
    select count(*) into l_valid from all_objects
     where owner = sys_context('USERENV','CURRENT_SCHEMA') and object_name = 'SLIC_ZKT_DELIVERY_V2'
       and object_type in ('PACKAGE','PACKAGE BODY') and status = 'VALID';
    select count(*) into l_errors from all_errors
     where owner = sys_context('USERENV','CURRENT_SCHEMA') and name = 'SLIC_ZKT_DELIVERY_V2'
       and attribute = 'ERROR';
    if l_valid <> 2 or l_errors <> 0 then
        raise_application_error(-20679, 'DELIVERY_READER_COMPILATION_FAILED');
    end if;
end;
/
