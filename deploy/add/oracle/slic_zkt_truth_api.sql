set define off

/*
  SLIC ZKT truth API for Oracle ORDS.

  Deployment notes:
  - Replace c_api_password_sha256 with the uppercase SHA-256 hex digest of the
    zone-agent password before running in Oracle. Never commit the password.
  - This script does not change HR_RAW_ATTN_CAPTURE_EVENTS table shape.
  - The reconcile endpoint is intentionally authoritative inside the supplied
    zone_id + device_serial + attendance-date window. API v2 rejects every
    request unless terminal-event and complete identity-map attestations match
    the submitted event array before any delete can run.
*/

create or replace package slic_zkt_truth_api authid definer as
    procedure post_live(p_body in clob);
    procedure post_bulk(p_body in clob);
    procedure post_check(p_body in clob);
    procedure post_reconcile(p_body in clob);

    function parse_event_timestamp(p_value in varchar2) return timestamp with time zone;
    function attendance_date_for(p_event_ts in timestamp with time zone) return date;
    function valid_capture_type(p_value in varchar2) return number;
    function valid_trust_status(p_value in varchar2) return number;
end slic_zkt_truth_api;
/

create or replace package body slic_zkt_truth_api as
    c_api_username constant varchar2(128) := 'slic_zone_agent';
    c_api_password_sha256 constant varchar2(64) := 'REPLACE_WITH_64_CHARACTER_SHA256_HEX';
    c_attendance_timezone constant varchar2(64) := 'Asia/Karachi';
    c_max_reconcile_days constant number := 45;

    e_response_sent exception;

    function json_escape(p_value in varchar2) return varchar2 is
        v_value varchar2(32767) := nvl(p_value, '');
    begin
        v_value := replace(v_value, '\', '\\');
        v_value := replace(v_value, '"', '\"');
        v_value := replace(v_value, chr(10), '\n');
        v_value := replace(v_value, chr(13), '\r');
        v_value := replace(v_value, chr(9), '\t');
        return v_value;
    end json_escape;

    procedure send_metrics(
        p_status in number,
        p_success in boolean,
        p_message in varchar2,
        p_received_count in number default 0,
        p_inserted_count in number default 0,
        p_deleted_count in number default 0,
        p_corrected_count in number default 0,
        p_duplicate_existing_count in number default 0,
        p_datasync_zero_count in number default 0,
        p_invalid_count in number default 0,
        p_conflicts_json in clob default '[]')
    is
        v_json clob;
        v_reason varchar2(64);
        v_success_json varchar2(5);
    begin
        v_success_json := case when p_success then 'true' else 'false' end;
        v_reason := case p_status
            when 200 then 'OK'
            when 201 then 'Created'
            when 400 then 'Bad Request'
            when 401 then 'Unauthorized'
            when 500 then 'Internal Server Error'
            else 'OK'
        end;

        select json_object(
                   'success' value v_success_json format json,
                   'message' value p_message,
                   'received_count' value nvl(p_received_count, 0),
                   'inserted_count' value nvl(p_inserted_count, 0),
                   'deleted_count' value nvl(p_deleted_count, 0),
                   'corrected_count' value nvl(p_corrected_count, 0),
                   'duplicate_existing_count' value nvl(p_duplicate_existing_count, 0),
                   'datasync_zero_count' value nvl(p_datasync_zero_count, 0),
                   'invalid_count' value nvl(p_invalid_count, 0),
                   'conflicts' value coalesce(p_conflicts_json, to_clob('[]')) format json
                   returning clob)
          into v_json
          from dual;

        owa_util.status_line(p_status, v_reason, false);
        owa_util.mime_header('application/json', false);
        owa_util.http_header_close;
        htp.p(dbms_lob.substr(v_json, 32767, 1));
    end send_metrics;

    procedure fail_and_stop(
        p_status in number,
        p_message in varchar2,
        p_received_count in number default 0,
        p_invalid_count in number default 0,
        p_conflicts_json in clob default '[]')
    is
    begin
        send_metrics(
            p_status => p_status,
            p_success => false,
            p_message => p_message,
            p_received_count => p_received_count,
            p_invalid_count => p_invalid_count,
            p_conflicts_json => p_conflicts_json);
        raise e_response_sent;
    end fail_and_stop;

    procedure require_auth is
        v_username varchar2(512);
        v_password varchar2(1024);

        function password_sha256(p_value in varchar2) return varchar2 is
            v_digest varchar2(64);
        begin
            select rawtohex(standard_hash(p_value, 'SHA256'))
              into v_digest
              from dual;
            return v_digest;
        end password_sha256;
    begin
        v_username := coalesce(
            owa_util.get_cgi_env('HTTP_X_API_USERNAME'),
            owa_util.get_cgi_env('X_API_USERNAME'),
            owa_util.get_cgi_env('X-API-Username'),
            owa_util.get_cgi_env('x-api-username'));
        v_password := coalesce(
            owa_util.get_cgi_env('HTTP_X_API_PASSWORD'),
            owa_util.get_cgi_env('X_API_PASSWORD'),
            owa_util.get_cgi_env('X-API-Password'),
            owa_util.get_cgi_env('x-api-password'));

        if nvl(v_username, chr(0)) <> c_api_username
           or password_sha256(
                  nvl(v_password, chr(0))
              ) <> c_api_password_sha256 then
            fail_and_stop(
                p_status => 401,
                p_message => 'Invalid API credentials',
                p_conflicts_json => '["auth_failed"]');
        end if;
    end require_auth;

    function parse_event_timestamp(p_value in varchar2) return timestamp with time zone is
        v_value varchar2(80) := trim(p_value);
    begin
        if v_value is null then
            return null;
        end if;

        v_value := regexp_replace(v_value, 'Z$', '+00:00', 1, 0, 'i');

        if regexp_like(v_value, '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}$') then
            return to_timestamp_tz(v_value, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM');
        elsif regexp_like(v_value, '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$') then
            return from_tz(to_timestamp(v_value, 'YYYY-MM-DD HH24:MI:SS'), c_attendance_timezone);
        end if;

        return null;
    exception
        when others then
            return null;
    end parse_event_timestamp;

    function attendance_date_for(p_event_ts in timestamp with time zone) return date is
    begin
        if p_event_ts is null then
            return null;
        end if;
        return trunc(cast(p_event_ts at time zone c_attendance_timezone as date));
    end attendance_date_for;

    function valid_capture_type(p_value in varchar2) return number is
    begin
        if p_value in ('LIVE', 'LIVE_POLL', 'DUMP_RECONNECT', 'DUMP_STARTUP', 'MANUAL_REPROCESS') then
            return 1;
        end if;
        return 0;
    end valid_capture_type;

    function valid_trust_status(p_value in varchar2) return number is
    begin
        if p_value in (
            'TRUSTED_LIVE',
            'INTERNET_OFFLINE_TRUSTED_LOCAL',
            'BACKFILL_ACCEPTED_CLOCK_OK',
            'BACKFILL_UNVERIFIED_BLIND_PERIOD',
            'SUSPECT_DEVICE_TIME'
        ) then
            return 1;
        end if;
        return 0;
    end valid_trust_status;

    function event_array_count(p_body in clob) return number is
        v_count number;
    begin
        select count(*)
          into v_count
          from json_table(
                   p_body,
                   '$.events[*]'
                   columns event_uid varchar2(150) path '$.event_uid' null on error
               );
        return v_count;
    exception
        when others then
            return -1;
    end event_array_count;

    procedure send_check_result(
        p_received_count in number,
        p_existing_count in number,
        p_missing_count in number,
        p_missing_event_uids in clob)
    is
        v_json clob;
    begin
        select json_object(
                   'success' value 'true' format json,
                   'received_count' value nvl(p_received_count, 0),
                   'existing_count' value nvl(p_existing_count, 0),
                   'missing_count' value nvl(p_missing_count, 0),
                   'missing_event_uids' value coalesce(
                       p_missing_event_uids,
                       to_clob('[]')) format json
                   returning clob)
          into v_json
          from dual;
        owa_util.status_line(200, 'OK', false);
        owa_util.mime_header('application/json', false);
        owa_util.http_header_close;
        htp.p(dbms_lob.substr(v_json, 32767, 1));
    end send_check_result;

    procedure handle_event_array(p_body in clob, p_allow_empty in boolean, p_default_capture_type in varchar2) is
        v_received number := 0;
        v_invalid number := 0;
        v_request_dupes number := 0;
        v_existing number := 0;
        v_inserted number := 0;
    begin
        require_auth;

        v_received := event_array_count(p_body);
        if v_received < 0 then
            fail_and_stop(400, 'Malformed JSON payload', 0, 1, '["malformed_json"]');
        end if;
        if v_received = 0 and not p_allow_empty then
            fail_and_stop(400, 'No events supplied', 0, 1, '["empty_events"]');
        end if;

        with incoming as (
            select trim(j.event_uid) event_uid,
                   trim(j.zone_id) zone_id,
                   trim(j.device_id) device_id,
                   coalesce(nullif(trim(j.device_serial), ''), 'unknown') device_serial,
                   trim(j.user_id) user_id,
                   trim(j.employee_name) employee_name,
                   trim(j.cnic) cnic,
                   coalesce(nullif(trim(j.capture_type), ''), p_default_capture_type) capture_type,
                   coalesce(nullif(trim(j.trust_status), ''), 'TRUSTED_LIVE') trust_status,
                   coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                   slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns
                           event_uid varchar2(150) path '$.event_uid' null on error,
                           zone_id varchar2(50) path '$.zone_id' null on error,
                           device_id varchar2(100) path '$.device_id' null on error,
                           device_serial varchar2(100) path '$.device_serial' null on error,
                           user_id varchar2(50) path '$.user_id' null on error,
                           employee_name varchar2(200) path '$.employee_name' null on error,
                           cnic varchar2(13) path '$.cnic' null on error,
                           event_timestamp varchar2(80) path '$.timestamp' null on error,
                           capture_type varchar2(30) path '$.capturetype' null on error,
                           trust_status varchar2(60) path '$.trust_status' null on error,
                           raw_punch varchar2(1) path '$.raw_punch' null on error
                   ) j
        )
        select count(*)
          into v_invalid
          from incoming
         where event_uid is null
            or zone_id is null
            or device_id is null
            or user_id is null
            or cnic is null
            or not regexp_like(cnic, '^[0-9]{13}$')
            or event_ts is null
            or raw_punch not in ('T', 'F')
            or slic_zkt_truth_api.valid_capture_type(capture_type) = 0
            or slic_zkt_truth_api.valid_trust_status(trust_status) = 0;

        select greatest(count(*) - count(distinct trim(j.event_uid)), 0)
          into v_request_dupes
          from json_table(
                   p_body,
                   '$.events[*]'
                   columns event_uid varchar2(150) path '$.event_uid' null on error
               ) j
         where trim(j.event_uid) is not null;

        if v_invalid > 0 or v_request_dupes > 0 then
            fail_and_stop(
                400,
                'Invalid or duplicate event payload',
                v_received,
                v_invalid + v_request_dupes,
                '["invalid_event_shape","duplicate_event_uid_in_request"]');
        end if;

        with incoming as (
            select trim(j.event_uid) event_uid
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns event_uid varchar2(150) path '$.event_uid' null on error
                   ) j
        )
        select count(*)
          into v_existing
          from incoming i
         where exists (
                   select 1
                     from hr_raw_attn_capture_events d
                    where d.event_uid = i.event_uid
               );

        insert into hr_raw_attn_capture_events (
            event_uid,
            zone_id,
            device_id,
            device_serial,
            user_id,
            employee_name,
            cnic,
            event_timestamp,
            clock_diff_seconds,
            capture_type,
            trust_status,
            received_at,
            attendance_date,
            check_in,
            check_out,
            raw_punch,
            datasync
        )
        with incoming as (
            select trim(j.event_uid) event_uid,
                   trim(j.zone_id) zone_id,
                   trim(j.device_id) device_id,
                   coalesce(nullif(trim(j.device_serial), ''), 'unknown') device_serial,
                   trim(j.user_id) user_id,
                   trim(j.employee_name) employee_name,
                   trim(j.cnic) cnic,
                   slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts,
                   to_number(j.clockdiff default null on conversion error) clock_diff_seconds,
                   coalesce(nullif(trim(j.capture_type), ''), p_default_capture_type) capture_type,
                   coalesce(nullif(trim(j.trust_status), ''), 'TRUSTED_LIVE') trust_status,
                   coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                   slic_zkt_truth_api.attendance_date_for(slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)) attendance_date
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns
                           event_uid varchar2(150) path '$.event_uid' null on error,
                           zone_id varchar2(50) path '$.zone_id' null on error,
                           device_id varchar2(100) path '$.device_id' null on error,
                           device_serial varchar2(100) path '$.device_serial' null on error,
                           user_id varchar2(50) path '$.user_id' null on error,
                           employee_name varchar2(200) path '$.employee_name' null on error,
                           cnic varchar2(13) path '$.cnic' null on error,
                           event_timestamp varchar2(80) path '$.timestamp' null on error,
                           clockdiff varchar2(40) path '$.clockdiff' null on error,
                           capture_type varchar2(30) path '$.capturetype' null on error,
                           trust_status varchar2(60) path '$.trust_status' null on error,
                           raw_punch varchar2(1) path '$.raw_punch' null on error
                   ) j
        ),
        normal_rank as (
            select event_uid,
                   row_number() over (partition by cnic, attendance_date order by event_ts, event_uid) rn_in,
                   row_number() over (partition by cnic, attendance_date order by event_ts desc, event_uid desc) rn_out,
                   count(*) over (partition by cnic, attendance_date) normal_count
              from incoming
             where raw_punch = 'F'
        ),
        flagged as (
            select i.*,
                   case when i.raw_punch = 'F' and n.rn_in = 1 then 'T' else 'F' end check_in,
                   case when i.raw_punch = 'F' and n.normal_count > 1 and n.rn_out = 1 then 'T' else 'F' end check_out
              from incoming i
              left join normal_rank n on n.event_uid = i.event_uid
        )
        select f.event_uid,
               f.zone_id,
               f.device_id,
               f.device_serial,
               f.user_id,
               f.employee_name,
               f.cnic,
               f.event_ts,
               f.clock_diff_seconds,
               f.capture_type,
               f.trust_status,
               systimestamp,
               f.attendance_date,
               f.check_in,
               f.check_out,
               f.raw_punch,
               0
          from flagged f
         where not exists (
                   select 1
                     from hr_raw_attn_capture_events d
                    where d.event_uid = f.event_uid
               );

        v_inserted := sql%rowcount;
        commit;

        send_metrics(
            p_status => case when v_inserted > 0 then 201 else 200 end,
            p_success => true,
            p_message => 'Events accepted',
            p_received_count => v_received,
            p_inserted_count => v_inserted,
            p_duplicate_existing_count => v_existing);
    exception
        when e_response_sent then
            rollback;
        when others then
            rollback;
            send_metrics(
                p_status => 500,
                p_success => false,
                p_message => 'Unhandled API error: ' || substr(sqlerrm, 1, 500),
                p_received_count => v_received,
                p_invalid_count => v_invalid,
                p_conflicts_json => '["server_error"]');
    end handle_event_array;

    procedure post_live(p_body in clob) is
        v_payload clob;
    begin
        v_payload := to_clob('{"events":[') || p_body || to_clob(']}');
        handle_event_array(v_payload, false, 'LIVE');
    end post_live;

    procedure post_bulk(p_body in clob) is
    begin
        handle_event_array(p_body, false, 'LIVE_POLL');
    end post_bulk;

    procedure post_check(p_body in clob) is
        v_received number := 0;
        v_invalid number := 0;
        v_request_dupes number := 0;
        v_missing number := 0;
        v_missing_event_uids clob;
    begin
        require_auth;
        begin
            select count(*),
                   sum(
                       case
                           when event_uid is null
                             or not regexp_like(event_uid, '^[0-9a-f]{64}$', 'c')
                           then 1
                           else 0
                       end)
              into v_received, v_invalid
              from json_table(
                       p_body,
                       '$.event_uids[*]'
                       columns event_uid varchar2(150) path '$' null on error
                   );
        exception
            when others then
                fail_and_stop(400, 'Malformed JSON payload', 0, 1, '["malformed_json"]');
        end;

        v_invalid := nvl(v_invalid, 0);
        if v_received < 1 or v_received > 500 then
            fail_and_stop(
                400,
                'Membership check requires between 1 and 500 event_uids',
                v_received,
                1,
                '["invalid_event_uid_count"]');
        end if;

        select greatest(count(*) - count(distinct event_uid), 0)
          into v_request_dupes
          from json_table(
                   p_body,
                   '$.event_uids[*]'
                   columns event_uid varchar2(150) path '$' null on error
               );
        if v_invalid > 0 or v_request_dupes > 0 then
            fail_and_stop(
                400,
                'Invalid or duplicate event_uid membership request',
                v_received,
                v_invalid + v_request_dupes,
                '["invalid_event_uid","duplicate_event_uid_in_request"]');
        end if;

        with incoming as (
            select j.event_uid
              from json_table(
                       p_body,
                       '$.event_uids[*]'
                       columns event_uid varchar2(150) path '$' null on error
                   ) j
        ),
        missing as (
            select i.event_uid
              from incoming i
             where not exists (
                       select 1
                         from hr_raw_attn_capture_events d
                        where d.event_uid = i.event_uid
                   )
        )
        select count(*),
               json_arrayagg(event_uid order by event_uid returning clob)
          into v_missing, v_missing_event_uids
          from missing;
        if v_missing_event_uids is null then
            v_missing_event_uids := to_clob('[]');
        end if;

        send_check_result(
            p_received_count => v_received,
            p_existing_count => v_received - v_missing,
            p_missing_count => v_missing,
            p_missing_event_uids => v_missing_event_uids);
    exception
        when e_response_sent then
            rollback;
        when others then
            rollback;
            send_metrics(
                p_status => 500,
                p_success => false,
                p_message => 'Unhandled membership check error: ' || substr(sqlerrm, 1, 500),
                p_received_count => v_received,
                p_invalid_count => v_invalid,
                p_conflicts_json => '["server_error"]');
    end post_check;

    procedure post_reconcile(p_body in clob) is
        v_api_version number;
        v_zone_id varchar2(50);
        v_device_id varchar2(100);
        v_device_serial varchar2(100);
        v_window_start date;
        v_window_end date;
        v_window_start_text varchar2(32);
        v_window_end_text varchar2(32);
        v_mode varchar2(80);
        v_terminal_event_count number;
        v_identity_mapped_count number;
        v_identity_map_complete varchar2(10);
        v_received number := 0;
        v_invalid number := 0;
        v_request_dupes number := 0;
        v_corrected number := 0;
        v_flag_corrected number := 0;
        v_deleted number := 0;
        v_inserted number := 0;
        v_existing number := 0;
        v_datasync_zero number := 0;
    begin
        require_auth;

        select json_value(p_body, '$.api_version' returning number null on error),
               trim(json_value(p_body, '$.zone_id' returning varchar2(50) null on error)),
               trim(json_value(p_body, '$.device_id' returning varchar2(100) null on error)),
               coalesce(nullif(trim(json_value(p_body, '$.device_serial' returning varchar2(100) null on error)), ''), 'unknown'),
               trim(json_value(p_body, '$.window_start' returning varchar2(32) null on error)),
               trim(json_value(p_body, '$.window_end' returning varchar2(32) null on error)),
               trim(json_value(p_body, '$.mode' returning varchar2(80) null on error)),
               json_value(p_body, '$.terminal_event_count' returning number null on error),
               json_value(p_body, '$.identity_mapped_count' returning number null on error),
               lower(trim(json_value(
                   p_body,
                   '$.identity_map_complete'
                   returning varchar2(10)
                   null on error)))
          into v_api_version,
               v_zone_id,
               v_device_id,
               v_device_serial,
               v_window_start_text,
               v_window_end_text,
               v_mode,
               v_terminal_event_count,
               v_identity_mapped_count,
               v_identity_map_complete
          from dual;

        begin
            v_window_start := to_date(v_window_start_text, 'YYYY-MM-DD');
            v_window_end := to_date(v_window_end_text, 'YYYY-MM-DD');
        exception
            when others then
                fail_and_stop(400, 'Invalid reconcile window', 0, 1, '["invalid_window"]');
        end;

        if v_api_version is null
           or v_api_version <> 2
           or v_zone_id is null
           or v_device_id is null
           or v_device_serial is null
           or v_mode is null
           or v_mode <> 'authoritative_replace'
           or v_window_start is null
           or v_window_end is null
           or v_window_end < v_window_start
           or v_window_end - v_window_start + 1 > c_max_reconcile_days then
            fail_and_stop(400, 'Invalid reconcile envelope', 0, 1, '["invalid_reconcile_envelope"]');
        end if;

        v_received := event_array_count(p_body);
        if v_received < 0 then
            fail_and_stop(400, 'Malformed JSON payload', 0, 1, '["malformed_json"]');
        end if;
        if v_identity_map_complete <> 'true'
           or v_terminal_event_count is null
           or v_identity_mapped_count is null
           or v_terminal_event_count <> v_received
           or v_identity_mapped_count <> v_received then
            fail_and_stop(
                400,
                'Incomplete terminal truth attestation; no destructive repair was performed',
                v_received,
                1,
                '["incomplete_terminal_truth"]');
        end if;

        with incoming as (
            select trim(j.event_uid) event_uid,
                   coalesce(nullif(trim(j.zone_id), ''), v_zone_id) zone_id,
                   coalesce(nullif(trim(j.device_id), ''), v_device_id) device_id,
                   coalesce(nullif(trim(j.device_serial), ''), v_device_serial) device_serial,
                   trim(j.user_id) user_id,
                   trim(j.employee_name) employee_name,
                   trim(j.cnic) cnic,
                   coalesce(nullif(trim(j.capture_type), ''), 'MANUAL_REPROCESS') capture_type,
                   coalesce(nullif(trim(j.trust_status), ''), 'TRUSTED_LIVE') trust_status,
                   coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                   slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts,
                   slic_zkt_truth_api.attendance_date_for(slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)) attendance_date
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns
                           event_uid varchar2(150) path '$.event_uid' null on error,
                           zone_id varchar2(50) path '$.zone_id' null on error,
                           device_id varchar2(100) path '$.device_id' null on error,
                           device_serial varchar2(100) path '$.device_serial' null on error,
                           user_id varchar2(50) path '$.user_id' null on error,
                           employee_name varchar2(200) path '$.employee_name' null on error,
                           cnic varchar2(13) path '$.cnic' null on error,
                           event_timestamp varchar2(80) path '$.timestamp' null on error,
                           capture_type varchar2(30) path '$.capturetype' null on error,
                           trust_status varchar2(60) path '$.trust_status' null on error,
                           raw_punch varchar2(1) path '$.raw_punch' null on error
                   ) j
        )
        select count(*)
          into v_invalid
          from incoming
         where event_uid is null
            or zone_id <> v_zone_id
            or device_id <> v_device_id
            or device_serial <> v_device_serial
            or user_id is null
            or cnic is null
            or not regexp_like(cnic, '^[0-9]{13}$')
            or event_ts is null
            or attendance_date not between v_window_start and v_window_end
            or raw_punch not in ('T', 'F')
            or slic_zkt_truth_api.valid_capture_type(capture_type) = 0
            or slic_zkt_truth_api.valid_trust_status(trust_status) = 0;

        select greatest(count(*) - count(distinct trim(j.event_uid)), 0)
          into v_request_dupes
          from json_table(
                   p_body,
                   '$.events[*]'
                   columns event_uid varchar2(150) path '$.event_uid' null on error
               ) j
         where trim(j.event_uid) is not null;

        if v_invalid > 0 or v_request_dupes > 0 then
            fail_and_stop(
                400,
                'Invalid reconcile event payload; no destructive repair was performed',
                v_received,
                v_invalid + v_request_dupes,
                '["invalid_event_shape","duplicate_event_uid_in_request"]');
        end if;

        delete from hr_raw_attn_capture_events d
         where d.zone_id = v_zone_id
           and coalesce(d.device_serial, 'unknown') = v_device_serial
           and d.attendance_date between v_window_start and v_window_end
           and exists (
                   with incoming as (
                       select trim(j.event_uid) event_uid,
                              coalesce(nullif(trim(j.device_id), ''), v_device_id) device_id,
                              trim(j.user_id) user_id,
                              trim(j.employee_name) employee_name,
                              trim(j.cnic) cnic,
                              coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                              slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts,
                              slic_zkt_truth_api.attendance_date_for(slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)) attendance_date
                         from json_table(
                                  p_body,
                                  '$.events[*]'
                                  columns
                                      event_uid varchar2(150) path '$.event_uid' null on error,
                                      device_id varchar2(100) path '$.device_id' null on error,
                                      user_id varchar2(50) path '$.user_id' null on error,
                                      employee_name varchar2(200) path '$.employee_name' null on error,
                                      cnic varchar2(13) path '$.cnic' null on error,
                                      event_timestamp varchar2(80) path '$.timestamp' null on error,
                                      raw_punch varchar2(1) path '$.raw_punch' null on error
                              ) j
                   )
                   select 1
                     from incoming i
                    where i.event_uid = d.event_uid
                      and (
                          nvl(d.device_id, chr(0)) <> i.device_id
                          or nvl(d.user_id, chr(0)) <> i.user_id
                          or nvl(d.employee_name, chr(0)) <> nvl(i.employee_name, chr(0))
                          or nvl(d.cnic, chr(0)) <> i.cnic
                          or d.raw_punch <> i.raw_punch
                          or d.attendance_date <> i.attendance_date
                          or abs((cast(d.event_timestamp at time zone 'UTC' as date) - cast(i.event_ts at time zone 'UTC' as date)) * 86400) > 0.5
                      )
               );
        v_corrected := sql%rowcount;

        delete from hr_raw_attn_capture_events d
         where d.zone_id = v_zone_id
           and coalesce(d.device_serial, 'unknown') = v_device_serial
           and d.attendance_date between v_window_start and v_window_end
           and not exists (
                   select 1
                     from json_table(
                              p_body,
                              '$.events[*]'
                              columns event_uid varchar2(150) path '$.event_uid' null on error
                          ) j
                    where trim(j.event_uid) = d.event_uid
               );
        v_deleted := sql%rowcount;

        with incoming as (
            select trim(j.event_uid) event_uid
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns event_uid varchar2(150) path '$.event_uid' null on error
                   ) j
        )
        select count(*)
          into v_existing
          from incoming i
         where exists (
                   select 1
                     from hr_raw_attn_capture_events d
                    where d.event_uid = i.event_uid
               );

        insert into hr_raw_attn_capture_events (
            event_uid,
            zone_id,
            device_id,
            device_serial,
            user_id,
            employee_name,
            cnic,
            event_timestamp,
            clock_diff_seconds,
            capture_type,
            trust_status,
            received_at,
            attendance_date,
            check_in,
            check_out,
            raw_punch,
            datasync
        )
        with incoming as (
            select trim(j.event_uid) event_uid,
                   v_zone_id zone_id,
                   v_device_id device_id,
                   v_device_serial device_serial,
                   trim(j.user_id) user_id,
                   trim(j.employee_name) employee_name,
                   trim(j.cnic) cnic,
                   slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts,
                   to_number(j.clockdiff default null on conversion error) clock_diff_seconds,
                   coalesce(nullif(trim(j.capture_type), ''), 'MANUAL_REPROCESS') capture_type,
                   coalesce(nullif(trim(j.trust_status), ''), 'TRUSTED_LIVE') trust_status,
                   coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                   slic_zkt_truth_api.attendance_date_for(slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)) attendance_date
              from json_table(
                       p_body,
                       '$.events[*]'
                       columns
                           event_uid varchar2(150) path '$.event_uid' null on error,
                           user_id varchar2(50) path '$.user_id' null on error,
                           employee_name varchar2(200) path '$.employee_name' null on error,
                           cnic varchar2(13) path '$.cnic' null on error,
                           event_timestamp varchar2(80) path '$.timestamp' null on error,
                           clockdiff varchar2(40) path '$.clockdiff' null on error,
                           capture_type varchar2(30) path '$.capturetype' null on error,
                           trust_status varchar2(60) path '$.trust_status' null on error,
                           raw_punch varchar2(1) path '$.raw_punch' null on error
                   ) j
        ),
        normal_rank as (
            select event_uid,
                   row_number() over (partition by cnic, attendance_date order by event_ts, event_uid) rn_in,
                   row_number() over (partition by cnic, attendance_date order by event_ts desc, event_uid desc) rn_out,
                   count(*) over (partition by cnic, attendance_date) normal_count
              from incoming
             where raw_punch = 'F'
        ),
        flagged as (
            select i.*,
                   case when i.raw_punch = 'F' and n.rn_in = 1 then 'T' else 'F' end check_in,
                   case when i.raw_punch = 'F' and n.normal_count > 1 and n.rn_out = 1 then 'T' else 'F' end check_out
              from incoming i
              left join normal_rank n on n.event_uid = i.event_uid
        )
        select f.event_uid,
               f.zone_id,
               f.device_id,
               f.device_serial,
               f.user_id,
               f.employee_name,
               f.cnic,
               f.event_ts,
               f.clock_diff_seconds,
               f.capture_type,
               f.trust_status,
               systimestamp,
               f.attendance_date,
               f.check_in,
               f.check_out,
               f.raw_punch,
               0
          from flagged f
         where not exists (
                   select 1
                     from hr_raw_attn_capture_events d
                    where d.event_uid = f.event_uid
               );
        v_inserted := sql%rowcount;

        merge into hr_raw_attn_capture_events d
        using (
            with incoming as (
                select trim(j.event_uid) event_uid,
                       trim(j.cnic) cnic,
                       slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) event_ts,
                       coalesce(nullif(trim(j.raw_punch), ''), 'F') raw_punch,
                       slic_zkt_truth_api.attendance_date_for(slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)) attendance_date
                  from json_table(
                           p_body,
                           '$.events[*]'
                           columns
                               event_uid varchar2(150) path '$.event_uid' null on error,
                               cnic varchar2(13) path '$.cnic' null on error,
                               event_timestamp varchar2(80) path '$.timestamp' null on error,
                               raw_punch varchar2(1) path '$.raw_punch' null on error
                       ) j
            ),
            normal_rank as (
                select event_uid,
                       row_number() over (partition by cnic, attendance_date order by event_ts, event_uid) rn_in,
                       row_number() over (partition by cnic, attendance_date order by event_ts desc, event_uid desc) rn_out,
                       count(*) over (partition by cnic, attendance_date) normal_count
                  from incoming
                 where raw_punch = 'F'
            )
            select i.event_uid,
                   i.raw_punch,
                   case when i.raw_punch = 'F' and n.rn_in = 1 then 'T' else 'F' end check_in,
                   case when i.raw_punch = 'F' and n.normal_count > 1 and n.rn_out = 1 then 'T' else 'F' end check_out
              from incoming i
              left join normal_rank n on n.event_uid = i.event_uid
        ) f
           on (d.event_uid = f.event_uid)
         when matched then update set
              d.check_in = f.check_in,
              d.check_out = f.check_out,
              d.raw_punch = f.raw_punch
          where d.check_in <> f.check_in
             or d.check_out <> f.check_out
             or d.raw_punch <> f.raw_punch;
        v_flag_corrected := sql%rowcount;

        update hr_raw_attn_capture_events d
           set d.datasync = 0
         where exists (
                   select 1
                     from json_table(
                              p_body,
                              '$.events[*]'
                              columns event_uid varchar2(150) path '$.event_uid' null on error
                          ) j
                    where trim(j.event_uid) = d.event_uid
               );
        v_datasync_zero := sql%rowcount;

        commit;

        send_metrics(
            p_status => 200,
            p_success => true,
            p_message => 'Authoritative ZKT truth reconcile applied',
            p_received_count => v_received,
            p_inserted_count => v_inserted,
            p_deleted_count => v_deleted,
            p_corrected_count => v_corrected + v_flag_corrected,
            p_duplicate_existing_count => v_existing,
            p_datasync_zero_count => v_datasync_zero,
            p_invalid_count => 0,
            p_conflicts_json => case
                when v_deleted + v_corrected + v_flag_corrected > 0 then
                    '["raw_table_repaired_from_zkt_truth"]'
                else
                    '[]'
            end);
    exception
        when e_response_sent then
            rollback;
        when others then
            rollback;
            send_metrics(
                p_status => 500,
                p_success => false,
                p_message => 'Unhandled reconcile error: ' || substr(sqlerrm, 1, 500),
                p_received_count => v_received,
                p_invalid_count => v_invalid,
                p_conflicts_json => '["server_error"]');
    end post_reconcile;
end slic_zkt_truth_api;
/

declare
    l_module_name varchar2(255);
begin
    begin
        select name
          into l_module_name
          from user_ords_modules
         where uri_prefix = '/raw_attn_capture_event/'
           and rownum = 1;
    exception
        when no_data_found then
            l_module_name := 'raw_attendance_capture';
            ords.define_module(
                p_module_name => l_module_name,
                p_base_path => '/raw_attn_capture_event/',
                p_items_per_page => 0,
                p_status => 'PUBLISHED',
                p_comments => 'ZKT raw attendance capture API with authoritative truth reconcile');
    end;

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/check');
    exception
        when dup_val_on_index then
            null;
    end;

    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/check',
        p_method => 'POST',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => q'[
begin
    slic_zkt_truth_api.post_check(:body_text);
end;
]');

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/reconcile');
    exception
        when dup_val_on_index then
            null;
    end;

    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/reconcile',
        p_method => 'POST',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => q'[
begin
    slic_zkt_truth_api.post_reconcile(:body_text);
end;
]');

    commit;
end;
/
