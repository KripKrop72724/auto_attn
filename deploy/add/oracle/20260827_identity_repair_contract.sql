set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  ADD-only, idempotent Oracle identity correction contract.

  Security/safety properties:
  - accepts only the dedicated ADD username/password digest below;
  - never returns Oracle employee name or CNIC;
  - operation ID + payload digest replay is immutable;
  - preserves EVENT_UID, DEVICE_SERIAL, USER_ID, EVENT_TIMESTAMP and RAW_PUNCH;
  - updates only EMPLOYEE_NAME/CNIC plus derived DATASYNC/check flags;
  - requires the site-specific downstream adapter before mutation;
  - commits a durable receipt in the same transaction.

  Run 20260827_identity_repair_preflight.sql first. Replace only the two ADD
  credential placeholders. Connector and fleet credentials are intentionally
  not present in this package.
*/

variable identity_repair_previous_spec clob
variable identity_repair_previous_body clob

declare
    l_valid number;
    l_spec clob;
    l_body clob;
begin
    :identity_repair_previous_spec := null;
    :identity_repair_previous_body := null;
    select count(*)
      into l_valid
      from user_objects
     where object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
       and object_type in ('PACKAGE', 'PACKAGE BODY')
       and status = 'VALID';
    if l_valid = 2 then
        dbms_lob.createtemporary(l_spec, true);
        dbms_lob.writeappend(l_spec, length('create or replace '), 'create or replace ');
        for source_line in (
            select text
              from user_source
             where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
               and type = 'PACKAGE'
             order by line
        ) loop
            dbms_lob.writeappend(l_spec, length(source_line.text), source_line.text);
        end loop;
        dbms_lob.createtemporary(l_body, true);
        dbms_lob.writeappend(l_body, length('create or replace '), 'create or replace ');
        for source_line in (
            select text
              from user_source
             where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
               and type = 'PACKAGE BODY'
             order by line
        ) loop
            dbms_lob.writeappend(l_body, length(source_line.text), source_line.text);
        end loop;
        :identity_repair_previous_spec := l_spec;
        :identity_repair_previous_body := l_body;
    end if;
end;
/

declare
    l_exists number;
begin
    select count(*) into l_exists
      from user_tables
     where table_name = 'SLIC_ZKT_ID_REPAIR_RECEIPTS';
    if l_exists = 0 then
        execute immediate q'~
            create table slic_zkt_id_repair_receipts (
                operation_id varchar2(36) not null,
                payload_digest varchar2(64) not null,
                event_uid varchar2(150) not null,
                connector_id varchar2(120) not null,
                terminal_serial varchar2(120) not null,
                desired_identity_digest varchar2(64) not null,
                before_content_token varchar2(64) not null,
                resulting_content_token varchar2(64) not null,
                action varchar2(20) not null,
                raw_content_verified char(1) default 'T' not null,
                immutable_facts_unchanged char(1) default 'T' not null,
                event_count_preserved char(1) default 'T' not null,
                event_uid_unique char(1) default 'T' not null,
                created_at timestamp with time zone default systimestamp not null,
                constraint pk_slic_zkt_id_repair primary key (operation_id),
                constraint ck_slic_zkt_id_repair_action
                    check (action in ('NOOP','INSERTED','UPDATED')),
                constraint ck_slic_zkt_id_repair_bool
                    check (
                        raw_content_verified = 'T'
                        and immutable_facts_unchanged = 'T'
                        and event_count_preserved = 'T'
                        and event_uid_unique = 'T'
                    )
            )
        ~';
        execute immediate q'~
            create index ix_slic_zkt_id_repair_event
                on slic_zkt_id_repair_receipts (event_uid, created_at)
        ~';
    end if;
end;
/

create or replace package slic_zkt_identity_repair_api authid definer as
    procedure get_capabilities;
    procedure post_check(p_body in clob);
    procedure post_repair(p_body in clob);
    procedure post_status(p_body in clob);
end slic_zkt_identity_repair_api;
/

create or replace package body slic_zkt_identity_repair_api as
    c_add_api_username constant varchar2(128) := 'REPLACE_WITH_ADD_API_USERNAME';
    c_add_api_password_sha256 constant varchar2(64) :=
        'REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX';
    c_contract_version constant varchar2(10) := '1';

    e_response_sent exception;

    function sha256(p_value in varchar2) return varchar2 is
        l_digest varchar2(64);
    begin
        select lower(rawtohex(standard_hash(nvl(p_value, chr(0)), 'SHA256')))
          into l_digest
          from dual;
        return l_digest;
    end sha256;

    function component(p_value in varchar2) return varchar2 is
    begin
        return nvl(p_value, chr(0));
    end component;

    function safe_component(p_value in varchar2) return boolean is
    begin
        return p_value is null
            or (instr(p_value, chr(0)) = 0 and instr(p_value, chr(31)) = 0);
    end safe_component;

    function operation_payload_digest(
        p_operation_id in varchar2,
        p_expected_token in varchar2,
        p_event_uid in varchar2,
        p_immutable_digest in varchar2,
        p_device_serial in varchar2,
        p_source_uid in varchar2,
        p_user_id in varchar2,
        p_event_timestamp in varchar2,
        p_punch in varchar2,
        p_status in varchar2,
        p_raw_punch in varchar2,
        p_source in varchar2,
        p_employee_name in varchar2,
        p_cnic in varchar2,
        p_identity_digest in varchar2,
        p_connector_id in varchar2,
        p_zone_id in varchar2,
        p_zone_name in varchar2,
        p_device_id in varchar2,
        p_capture_type in varchar2,
        p_trust_status in varchar2,
        p_clock_diff_seconds in varchar2
    ) return varchar2 is
    begin
        return sha256(
            component(c_contract_version) || chr(31)
            || component(p_operation_id) || chr(31)
            || component(p_expected_token) || chr(31)
            || component(p_event_uid) || chr(31)
            || component(p_immutable_digest) || chr(31)
            || component(p_device_serial) || chr(31)
            || component(p_source_uid) || chr(31)
            || component(p_user_id) || chr(31)
            || component(p_event_timestamp) || chr(31)
            || component(p_punch) || chr(31)
            || component(p_status) || chr(31)
            || component(p_raw_punch) || chr(31)
            || component(p_source) || chr(31)
            || component(p_employee_name) || chr(31)
            || component(p_cnic) || chr(31)
            || component(p_identity_digest) || chr(31)
            || component(p_connector_id) || chr(31)
            || component(p_zone_id) || chr(31)
            || component(p_zone_name) || chr(31)
            || component(p_device_id) || chr(31)
            || component(p_capture_type) || chr(31)
            || component(p_trust_status) || chr(31)
            || component(p_clock_diff_seconds)
        );
    end operation_payload_digest;

    function bool_json(p_value in boolean) return varchar2 is
    begin
        return case when p_value then 'true' else 'false' end;
    end bool_json;

    procedure send_json(p_status in number, p_body in clob) is
        l_reason varchar2(64);
        l_offset pls_integer := 1;
        l_length pls_integer;
    begin
        l_reason := case p_status
            when 200 then 'OK'
            when 201 then 'Created'
            when 400 then 'Bad Request'
            when 401 then 'Unauthorized'
            when 403 then 'Forbidden'
            when 409 then 'Conflict'
            when 500 then 'Internal Server Error'
            else 'OK'
        end;
        owa_util.status_line(p_status, l_reason, false);
        owa_util.mime_header('application/json', false);
        owa_util.http_header_close;
        l_length := dbms_lob.getlength(p_body);
        while l_offset <= l_length loop
            htp.prn(dbms_lob.substr(p_body, 30000, l_offset));
            l_offset := l_offset + 30000;
        end loop;
    end send_json;

    procedure fail_and_stop(p_status in number, p_code in varchar2) is
        l_json clob;
    begin
        select json_object(
                   'success' value 'false' format json,
                   'error_code' value p_code
                   returning clob
               )
          into l_json
          from dual;
        send_json(p_status, l_json);
        raise e_response_sent;
    end fail_and_stop;

    procedure require_add_auth is
        l_username varchar2(512);
        l_password varchar2(1024);
    begin
        l_username := coalesce(
            owa_util.get_cgi_env('HTTP_X_API_USERNAME'),
            owa_util.get_cgi_env('X_API_USERNAME'),
            owa_util.get_cgi_env('X-API-Username'),
            owa_util.get_cgi_env('x-api-username'));
        l_password := coalesce(
            owa_util.get_cgi_env('HTTP_X_API_PASSWORD'),
            owa_util.get_cgi_env('X_API_PASSWORD'),
            owa_util.get_cgi_env('X-API-Password'),
            owa_util.get_cgi_env('x-api-password'));
        if nvl(l_username, chr(0)) <> c_add_api_username
           or sha256(nvl(l_password, chr(0))) <> lower(c_add_api_password_sha256) then
            fail_and_stop(401, 'ADD_ONLY_AUTH_REQUIRED');
        end if;
    end require_add_auth;

    function downstream_ready return boolean is
        l_package number;
        l_view number;
        l_columns number;
    begin
        select count(*) into l_package
          from user_objects
         where object_name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
           and object_type = 'PACKAGE BODY'
           and status = 'VALID';
        select count(*) into l_view
          from user_objects
         where object_name = 'SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS'
           and object_type = 'VIEW'
           and status = 'VALID';
        select count(*) into l_columns
          from user_tab_columns
         where table_name = 'SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS'
           and column_name in (
               'OPERATION_ID', 'IDENTITY_DIGEST', 'DOWNSTREAM_VERIFIED',
               'STALE_OLD_IDENTITY_ABSENT', 'OBSERVED_AT'
           );
        return l_package = 1 and l_view = 1 and l_columns = 5;
    end downstream_ready;

    function content_token(p_event_uid in varchar2) return varchar2 is
        l_token varchar2(64);
        l_material varchar2(32767);
    begin
        select d.event_uid || chr(31) || d.device_serial || chr(31)
                   || d.user_id || chr(31)
                   || to_char(d.event_timestamp at time zone 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"') || chr(31)
                   || d.raw_punch || chr(31) || nvl(d.employee_name, chr(0))
                   || chr(31) || nvl(d.cnic, chr(0))
          into l_material
          from hr_raw_attn_capture_events d
         where d.event_uid = p_event_uid;
        l_token := sha256(l_material);
        return l_token;
    exception
        when no_data_found then
            return sha256('MISSING' || chr(31) || p_event_uid);
    end content_token;

    function immutable_matches(
        p_event_uid in varchar2,
        p_terminal_serial in varchar2,
        p_user_id in varchar2,
        p_timestamp in varchar2,
        p_raw_punch in varchar2
    ) return boolean is
        l_count number;
    begin
        select count(*)
          into l_count
          from hr_raw_attn_capture_events d
         where d.event_uid = p_event_uid
           and d.device_serial = p_terminal_serial
           and d.user_id = p_user_id
           and d.event_timestamp = slic_zkt_truth_api.parse_event_timestamp(p_timestamp)
           and d.raw_punch = p_raw_punch;
        return l_count = 1;
    end immutable_matches;

    procedure get_capabilities is
        l_json clob;
        l_ready boolean;
        l_ready_json varchar2(5);
    begin
        require_add_auth;
        l_ready := downstream_ready;
        l_ready_json := bool_json(l_ready);
        select json_object(
                   'contract_version' value c_contract_version,
                   'add_only_auth' value 'true' format json,
                   'content_preconditions' value 'true' format json,
                   'operation_replay' value 'true' format json,
                   'raw_content_verification' value 'true' format json,
                   'downstream_verification' value l_ready_json format json,
                   'old_identity_absence_verification' value l_ready_json format json,
                   'batch_limit' value 100,
                   'execution_ready' value l_ready_json format json
                   returning clob
               )
          into l_json
          from dual;
        send_json(200, l_json);
    exception
        when e_response_sent then null;
        when others then
            send_json(500, '{"success":false,"error_code":"CAPABILITY_ERROR"}');
    end get_capabilities;

    procedure post_check(p_body in clob) is
        l_results json_array_t := json_array_t();
        l_result json_object_t;
        l_count number;
        l_classification varchar2(40);
        l_token varchar2(64);
        l_stored_name hr_raw_attn_capture_events.employee_name%type;
        l_stored_cnic hr_raw_attn_capture_events.cnic%type;
        l_stored_serial hr_raw_attn_capture_events.device_serial%type;
        l_json clob;
    begin
        require_add_auth;
        if json_value(p_body, '$.contract_version') <> c_contract_version then
            fail_and_stop(400, 'CONTRACT_VERSION_UNSUPPORTED');
        end if;
        select count(*) into l_count
          from json_table(
                   p_body,
                   '$.items[*]' columns (x varchar2(1) path '$')
               );
        if l_count < 1 or l_count > 100 then
            fail_and_stop(400, 'BATCH_LIMIT');
        end if;

        for item in (
            select j.event_uid event_uid,
                   j.device_serial device_serial,
                   j.user_id user_id,
                   j.event_timestamp event_timestamp,
                   j.raw_punch raw_punch,
                   j.employee_name employee_name,
                   j.cnic cnic
              from json_table(
                       p_body,
                       '$.items[*]'
                       columns (
                           event_uid varchar2(150) path '$.event_uid' error on error,
                           device_serial varchar2(120) path '$.immutable_facts.device_serial' error on error,
                           user_id varchar2(100) path '$.immutable_facts.source_user_id' error on error,
                           event_timestamp varchar2(80) path '$.immutable_facts.device_event_time' error on error,
                           raw_punch varchar2(1) path '$.immutable_facts.raw_punch' error on error,
                           employee_name varchar2(200) path '$.desired_identity.employee_name' error on error,
                           cnic varchar2(13) path '$.desired_identity.cnic' error on error
                       )
                   ) j
        ) loop
            if not regexp_like(item.event_uid, '^[0-9a-f]{64}$', 'c')
               or item.device_serial is null
               or item.user_id is null
               or item.event_timestamp is null
               or item.raw_punch not in ('T','F')
               or item.employee_name is null
               or not regexp_like(item.cnic, '^[0-9]{13}$', 'c')
               or slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp) is null then
                fail_and_stop(400, 'INVALID_CHECK_ITEM');
            end if;
            select count(*) into l_count
              from hr_raw_attn_capture_events
             where event_uid = item.event_uid;
            if l_count = 0 then
                l_classification := 'MISSING';
                l_token := content_token(item.event_uid);
            elsif l_count > 1 then
                l_classification := 'CROSS_DEVICE_UID_COLLISION';
                l_token := null;
            else
                l_token := content_token(item.event_uid);
                select employee_name, cnic, device_serial
                  into l_stored_name, l_stored_cnic, l_stored_serial
                  from hr_raw_attn_capture_events
                 where event_uid = item.event_uid;
                if l_stored_serial <> item.device_serial then
                    l_classification := 'CROSS_DEVICE_UID_COLLISION';
                elsif not immutable_matches(
                    item.event_uid, item.device_serial, item.user_id,
                    item.event_timestamp, item.raw_punch
                ) then
                    l_classification := 'IMMUTABLE_MISMATCH';
                elsif nvl(l_stored_name, chr(0)) = nvl(item.employee_name, chr(0))
                      and l_stored_cnic = item.cnic then
                    l_classification := 'MATCH';
                else
                    l_classification := 'MISMATCH';
                end if;
            end if;
            l_result := json_object_t();
            l_result.put('event_uid', item.event_uid);
            l_result.put('classification', l_classification);
            if l_classification in ('MATCH','MISSING','MISMATCH') then
                l_result.put('current_content_token', l_token);
            end if;
            l_results.append(l_result);
        end loop;
        l_json := '{"success":true,"results":' || l_results.to_clob || '}';
        send_json(200, l_json);
    exception
        when e_response_sent then rollback;
        when others then
            rollback;
            send_json(400, '{"success":false,"error_code":"CHECK_VALIDATION_FAILED"}');
    end post_check;

    procedure post_repair(p_body in clob) is
        l_count number;
        l_before_count number;
        l_after_count number;
        l_existing number;
        l_token varchar2(64);
        l_result_token varchar2(64);
        l_action varchar2(20);
        l_old_cnic hr_raw_attn_capture_events.cnic%type;
        l_old_name hr_raw_attn_capture_events.employee_name%type;
        l_old_datasync hr_raw_attn_capture_events.datasync%type;
        l_stored_serial hr_raw_attn_capture_events.device_serial%type;
        l_days clob;
        l_results json_array_t := json_array_t();
        l_result json_object_t;
        l_json clob;
    begin
        require_add_auth;
        if not downstream_ready then
            fail_and_stop(409, 'DOWNSTREAM_ADAPTER_NOT_READY');
        end if;
        if json_value(p_body, '$.contract_version') <> c_contract_version then
            fail_and_stop(400, 'CONTRACT_VERSION_UNSUPPORTED');
        end if;
        select count(*) into l_count
          from json_table(
                   p_body,
                   '$.items[*]' columns (x varchar2(1) path '$')
               );
        if l_count < 1 or l_count > 100 then
            fail_and_stop(400, 'BATCH_LIMIT');
        end if;
        select greatest(count(*) - count(distinct operation_id), 0)
          into l_count
          from json_table(
                   p_body, '$.items[*]'
                   columns (operation_id varchar2(36) path '$.operation_id' error on error)
               );
        if l_count <> 0 then
            fail_and_stop(400, 'DUPLICATE_OPERATION_ID');
        end if;

        for item in (
            select j.operation_id operation_id,
                   j.payload_digest payload_digest,
                   j.expected_token expected_token,
                   j.event_uid event_uid,
                   j.immutable_digest immutable_digest,
                   j.device_serial device_serial,
                   j.source_uid source_uid,
                   j.user_id user_id,
                   j.event_timestamp event_timestamp,
                   j.punch punch,
                   j.status status,
                   j.raw_punch raw_punch,
                   j.source source,
                   j.employee_name employee_name,
                   j.cnic cnic,
                   j.identity_digest identity_digest,
                   j.connector_id connector_id,
                   j.zone_id zone_id,
                   j.zone_name zone_name,
                   j.device_id device_id,
                   j.capture_type capture_type,
                   j.trust_status trust_status,
                   j.clock_diff_seconds clock_diff_seconds
              from json_table(
                       p_body,
                       '$.items[*]'
                       columns (
                           operation_id varchar2(36) path '$.operation_id' error on error,
                           payload_digest varchar2(64) path '$.payload_digest' error on error,
                           expected_token varchar2(64) path '$.expected_content_token' error on error,
                           event_uid varchar2(150) path '$.event_uid' error on error,
                           immutable_digest varchar2(64) path '$.immutable_facts_digest' error on error,
                           device_serial varchar2(120) path '$.immutable_facts.device_serial' error on error,
                           source_uid varchar2(40) path '$.immutable_facts.source_uid' null on error,
                           user_id varchar2(100) path '$.immutable_facts.source_user_id' error on error,
                           event_timestamp varchar2(80) path '$.immutable_facts.device_event_time' error on error,
                           punch varchar2(40) path '$.immutable_facts.punch' null on error,
                           status varchar2(40) path '$.immutable_facts.status' null on error,
                           raw_punch varchar2(1) path '$.immutable_facts.raw_punch' error on error,
                           source varchar2(40) path '$.immutable_facts.source' error on error,
                           employee_name varchar2(200) path '$.desired_identity.employee_name' error on error,
                           cnic varchar2(13) path '$.desired_identity.cnic' error on error,
                           identity_digest varchar2(64) path '$.desired_identity.identity_digest' error on error,
                           connector_id varchar2(120) path '$.connector_id' error on error,
                           zone_id varchar2(50) path '$.insert_facts.zone_id' error on error,
                           zone_name varchar2(200) path '$.insert_facts.zone_name' error on error,
                           device_id varchar2(100) path '$.insert_facts.device_id' error on error,
                           capture_type varchar2(30) path '$.insert_facts.capture_type' error on error,
                           trust_status varchar2(60) path '$.insert_facts.trust_status' error on error,
                           clock_diff_seconds varchar2(80) path '$.insert_facts.clock_diff_seconds' null on error
                       )
                   ) j
        ) loop
            if not regexp_like(
                       item.operation_id,
                       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                       'i'
                   )
               or not regexp_like(item.payload_digest, '^[0-9a-f]{64}$', 'c')
               or not regexp_like(item.expected_token, '^[0-9a-f]{64}$', 'c')
               or not regexp_like(item.event_uid, '^[0-9a-f]{64}$', 'c')
               or not regexp_like(item.immutable_digest, '^[0-9a-f]{64}$', 'c')
               or not regexp_like(item.identity_digest, '^[0-9a-f]{64}$', 'c')
               or not regexp_like(item.cnic, '^[0-9]{13}$', 'c')
               or item.employee_name is null
               or item.device_serial is null
               or item.user_id is null
               or item.event_timestamp is null
               or item.source is null
               or item.connector_id is null
               or item.zone_id is null
               or item.device_id is null
               or item.raw_punch not in ('T','F')
               or slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp) is null
               or slic_zkt_truth_api.valid_capture_type(item.capture_type) <> 1
               or slic_zkt_truth_api.valid_trust_status(item.trust_status) <> 1
               or (
                   item.clock_diff_seconds is not null
                   and not regexp_like(
                       item.clock_diff_seconds,
                       '^-?[0-9]{1,12}\.[0-9]{6}$',
                       'c'
                   )
               )
               or item.connector_id <> json_value(p_body, '$.connector_id')
               or item.device_serial <> json_value(p_body, '$.terminal_serial')
               or not safe_component(item.operation_id)
               or not safe_component(item.expected_token)
               or not safe_component(item.event_uid)
               or not safe_component(item.immutable_digest)
               or not safe_component(item.device_serial)
               or not safe_component(item.source_uid)
               or not safe_component(item.user_id)
               or not safe_component(item.event_timestamp)
               or not safe_component(item.punch)
               or not safe_component(item.status)
               or not safe_component(item.raw_punch)
               or not safe_component(item.source)
               or not safe_component(item.employee_name)
               or not safe_component(item.cnic)
               or not safe_component(item.identity_digest)
               or not safe_component(item.connector_id)
               or not safe_component(item.zone_id)
               or not safe_component(item.zone_name)
               or not safe_component(item.device_id)
               or not safe_component(item.capture_type)
               or not safe_component(item.trust_status)
               or not safe_component(item.clock_diff_seconds)
               or item.payload_digest <> operation_payload_digest(
                   item.operation_id,
                   item.expected_token,
                   item.event_uid,
                   item.immutable_digest,
                   item.device_serial,
                   item.source_uid,
                   item.user_id,
                   item.event_timestamp,
                   item.punch,
                   item.status,
                   item.raw_punch,
                   item.source,
                   item.employee_name,
                   item.cnic,
                   item.identity_digest,
                   item.connector_id,
                   item.zone_id,
                   item.zone_name,
                   item.device_id,
                   item.capture_type,
                   item.trust_status,
                   item.clock_diff_seconds
               ) then
                fail_and_stop(400, 'INVALID_REPAIR_ITEM');
            end if;

            select count(*) into l_existing
              from slic_zkt_id_repair_receipts
             where operation_id = item.operation_id;
            if l_existing = 1 then
                select count(*) into l_count
                  from slic_zkt_id_repair_receipts
                 where operation_id = item.operation_id
                   and payload_digest = item.payload_digest;
                if l_count <> 1 then
                    fail_and_stop(409, 'OPERATION_ID_CONTENT_CONFLICT');
                end if;
                continue;
            end if;

            l_old_cnic := null;
            l_old_name := null;
            l_old_datasync := null;
            l_stored_serial := null;
            select count(*) into l_count
              from hr_raw_attn_capture_events
             where event_uid = item.event_uid;
            l_before_count := l_count;
            if l_count > 1 then
                l_result := json_object_t();
                l_result.put('operation_id', item.operation_id);
                l_result.put('event_uid', item.event_uid);
                l_result.put('state', 'REVIEW_REQUIRED');
                l_result.put('error_code', 'CROSS_DEVICE_UID_COLLISION');
                l_results.append(l_result);
                continue;
            elsif l_count = 1 then
                select cnic, employee_name, nvl(datasync, 0), device_serial
                  into l_old_cnic, l_old_name, l_old_datasync, l_stored_serial
                  from hr_raw_attn_capture_events
                 where event_uid = item.event_uid
                   for update;
                if l_stored_serial <> item.device_serial then
                    l_result := json_object_t();
                    l_result.put('operation_id', item.operation_id);
                    l_result.put('event_uid', item.event_uid);
                    l_result.put('state', 'REVIEW_REQUIRED');
                    l_result.put('error_code', 'CROSS_DEVICE_UID_COLLISION');
                    l_results.append(l_result);
                    continue;
                end if;
                if not immutable_matches(
                    item.event_uid, item.device_serial, item.user_id,
                    item.event_timestamp, item.raw_punch
                ) then
                    l_result := json_object_t();
                    l_result.put('operation_id', item.operation_id);
                    l_result.put('event_uid', item.event_uid);
                    l_result.put('state', 'REVIEW_REQUIRED');
                    l_result.put('error_code', 'IMMUTABLE_FACT_MISMATCH');
                    l_results.append(l_result);
                    continue;
                end if;
            end if;
            l_token := content_token(item.event_uid);
            if l_token <> item.expected_token then
                l_result := json_object_t();
                l_result.put('operation_id', item.operation_id);
                l_result.put('event_uid', item.event_uid);
                l_result.put('state', 'PRECONDITION_FAILED');
                l_result.put('error_code', 'CONTENT_PRECONDITION_MISMATCH');
                l_results.append(l_result);
                continue;
            end if;

            savepoint attendance_repair_item;
            begin
                execute immediate
                    'begin slic_zkt_downstream_repair.assert_repairable('
                    || ':event_uid,:old_cnic,:new_cnic,:attendance_date,'
                    || ':old_datasync); end;'
                    using item.event_uid, l_old_cnic, item.cnic,
                          slic_zkt_truth_api.attendance_date_for(
                              slic_zkt_truth_api.parse_event_timestamp(
                                  item.event_timestamp
                              )
                          ),
                          l_old_datasync;
            exception
                when others then
                    if sqlcode between -20549 and -20540 then
                        rollback to attendance_repair_item;
                        l_result := json_object_t();
                        l_result.put('operation_id', item.operation_id);
                        l_result.put('event_uid', item.event_uid);
                        l_result.put('state', 'REVIEW_REQUIRED');
                        l_result.put(
                            'error_code',
                            'DOWNSTREAM_PRECONDITION_UNSAFE'
                        );
                        l_results.append(l_result);
                        continue;
                    end if;
                    raise;
            end;

            if l_count = 0 then
                insert into hr_raw_attn_capture_events (
                    event_uid, zone_id, device_id, device_serial, user_id,
                    employee_name, cnic, event_timestamp, clock_diff_seconds,
                    capture_type, trust_status, received_at, attendance_date,
                    check_in, check_out, raw_punch, datasync
                ) values (
                    item.event_uid, item.zone_id, item.device_id, item.device_serial,
                    item.user_id, item.employee_name, item.cnic,
                    slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp),
                    case
                        when item.clock_diff_seconds is null then null
                        else to_number(
                            item.clock_diff_seconds,
                            '999999999999D999999',
                            'NLS_NUMERIC_CHARACTERS=''.,'''
                        )
                    end,
                    item.capture_type, item.trust_status,
                    systimestamp,
                    slic_zkt_truth_api.attendance_date_for(
                        slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp)
                    ),
                    'F', 'F', item.raw_punch, 0
                );
                l_action := 'INSERTED';
            else
                if nvl(l_old_name, chr(0)) = nvl(item.employee_name, chr(0))
                   and l_old_cnic = item.cnic then
                    update hr_raw_attn_capture_events set datasync = 0
                     where event_uid = item.event_uid;
                    l_action := 'NOOP';
                else
                    update hr_raw_attn_capture_events set employee_name = item.employee_name,
                           cnic = item.cnic,
                           datasync = 0
                     where event_uid = item.event_uid;
                    l_action := 'UPDATED';
                end if;
            end if;

            select json_object(
                       'events' value json_array(
                           json_object(
                               'cnic' value nvl(l_old_cnic, item.cnic),
                               'timestamp' value item.event_timestamp
                           ),
                           json_object(
                               'cnic' value item.cnic,
                               'timestamp' value item.event_timestamp
                           )
                       ) returning clob
                   )
              into l_days
              from dual;
            slic_zkt_recompute_daily_flags(l_days);

            execute immediate
                'begin slic_zkt_downstream_repair.repair_identity('
                || ':operation_id,:event_uid,:old_cnic,:new_cnic,'
                || ':attendance_date,:old_datasync); end;'
                using item.operation_id, item.event_uid, l_old_cnic, item.cnic,
                      slic_zkt_truth_api.attendance_date_for(
                          slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp)
                      ),
                      l_old_datasync;

            select count(*) into l_after_count
              from hr_raw_attn_capture_events
             where event_uid = item.event_uid;
            if l_after_count <> 1
               or l_after_count <> l_before_count + case when l_action = 'INSERTED' then 1 else 0 end
               or not immutable_matches(
                    item.event_uid, item.device_serial, item.user_id,
                    item.event_timestamp, item.raw_punch
               ) then
                fail_and_stop(409, 'POST_MUTATION_IMMUTABLE_ASSERTION_FAILED');
            end if;
            select count(*) into l_count
              from hr_raw_attn_capture_events
             where event_uid = item.event_uid
               and employee_name = item.employee_name
               and cnic = item.cnic;
            if l_count <> 1 then
                fail_and_stop(409, 'POST_MUTATION_CONTENT_ASSERTION_FAILED');
            end if;
            l_result_token := content_token(item.event_uid);
            insert into slic_zkt_id_repair_receipts (
                operation_id, payload_digest, event_uid, connector_id,
                terminal_serial, desired_identity_digest, before_content_token,
                resulting_content_token, action
            ) values (
                item.operation_id, item.payload_digest, item.event_uid,
                json_value(p_body, '$.connector_id'), item.device_serial,
                item.identity_digest, l_token, l_result_token, l_action
            );
        end loop;
        commit;

        for receipt in (
            select r.*
              from slic_zkt_id_repair_receipts r
              join json_table(
                       p_body, '$.items[*]'
                       columns (operation_id varchar2(36) path '$.operation_id')
                   ) requested
                on requested.operation_id = r.operation_id
             order by r.operation_id
        ) loop
            l_result := json_object_t();
            l_result.put('operation_id', receipt.operation_id);
            l_result.put('event_uid', receipt.event_uid);
            l_result.put('state', 'COMMITTED');
            l_result.put('action', receipt.action);
            l_result.put('receipt_id', receipt.operation_id);
            l_result.put('current_content_token', receipt.resulting_content_token);
            l_result.put('identity_digest', receipt.desired_identity_digest);
            l_result.put('raw_content_verified', true);
            l_result.put('immutable_facts_unchanged', true);
            l_result.put('event_count_preserved', true);
            l_result.put('event_uid_unique', true);
            l_results.append(l_result);
        end loop;
        l_json := '{"success":true,"results":' || l_results.to_clob || '}';
        send_json(200, l_json);
    exception
        when e_response_sent then rollback;
        when others then
            rollback;
            send_json(500, '{"success":false,"error_code":"REPAIR_TRANSACTION_FAILED"}');
    end post_repair;

    procedure post_status(p_body in clob) is
        l_results json_array_t := json_array_t();
        l_result json_object_t;
        l_count number;
        l_downstream_verified varchar2(1);
        l_stale_absent varchar2(1);
        l_downstream_digest varchar2(64);
        l_downstream_observed_at timestamp with time zone;
        l_json clob;
    begin
        require_add_auth;
        if json_value(p_body, '$.contract_version') <> c_contract_version then
            fail_and_stop(400, 'CONTRACT_VERSION_UNSUPPORTED');
        end if;
        select count(*) into l_count
          from json_table(
                   p_body,
                   '$.operation_ids[*]' columns (x varchar2(1) path '$')
               );
        if l_count < 1 or l_count > 100 then
            fail_and_stop(400, 'BATCH_LIMIT');
        end if;
        select count(*)
          into l_count
          from json_table(
                   p_body,
                   '$.operation_ids[*]'
                   columns (operation_id varchar2(36) path '$' error on error)
               ) requested
         where not regexp_like(
                   trim(requested.operation_id),
                   '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                   'i'
               );
        if l_count <> 0 then
            fail_and_stop(400, 'INVALID_OPERATION_ID');
        end if;
        select greatest(count(*) - count(distinct trim(operation_id)), 0)
          into l_count
          from json_table(
                   p_body,
                   '$.operation_ids[*]'
                   columns (operation_id varchar2(36) path '$' error on error)
               );
        if l_count <> 0 then
            fail_and_stop(400, 'DUPLICATE_OPERATION_ID');
        end if;
        for requested in (
            select trim(operation_id) operation_id
              from json_table(
                       p_body, '$.operation_ids[*]'
                       columns (operation_id varchar2(36) path '$')
                   )
        ) loop
            l_result := json_object_t();
            l_result.put('operation_id', requested.operation_id);
            select count(*) into l_count
              from slic_zkt_id_repair_receipts
             where operation_id = requested.operation_id;
            if l_count = 0 then
                l_result.put('state', 'NOT_FOUND');
            else
                for receipt in (
                    select *
                      from slic_zkt_id_repair_receipts
                     where operation_id = requested.operation_id
                ) loop
                    l_result.put('event_uid', receipt.event_uid);
                    l_result.put('state', 'COMMITTED');
                    l_result.put('action', receipt.action);
                    l_result.put('receipt_id', receipt.operation_id);
                    l_result.put('current_content_token', receipt.resulting_content_token);
                    l_result.put('identity_digest', receipt.desired_identity_digest);
                    l_result.put('raw_content_verified',
                        content_token(receipt.event_uid) = receipt.resulting_content_token);
                    l_result.put('immutable_facts_unchanged', true);
                    l_result.put('event_count_preserved', true);
                    l_result.put('event_uid_unique', true);
                    l_downstream_verified := 'F';
                    l_stale_absent := 'F';
                    l_downstream_digest := null;
                    l_downstream_observed_at := null;
                    if downstream_ready then
                        begin
                            execute immediate
                                'select downstream_verified, stale_old_identity_absent, '
                                || 'identity_digest, observed_at '
                                || 'from slic_zkt_repair_downstream_status '
                                || 'where operation_id = :operation_id'
                                into l_downstream_verified, l_stale_absent,
                                     l_downstream_digest, l_downstream_observed_at
                                using receipt.operation_id;
                        exception
                            when no_data_found then null;
                        end;
                    end if;
                    l_result.put('downstream_verified',
                        l_downstream_verified = 'T'
                        and l_downstream_digest = receipt.desired_identity_digest
                        and l_downstream_observed_at is not null
                        and l_downstream_observed_at >= receipt.created_at);
                    l_result.put('stale_old_identity_absent',
                        l_stale_absent = 'T'
                        and l_downstream_observed_at is not null
                        and l_downstream_observed_at >= receipt.created_at);
                end loop;
            end if;
            l_results.append(l_result);
        end loop;
        l_json := '{"success":true,"results":' || l_results.to_clob || '}';
        send_json(200, l_json);
    exception
        when e_response_sent then rollback;
        when others then
            rollback;
            send_json(500, '{"success":false,"error_code":"STATUS_FAILED"}');
    end post_status;
end slic_zkt_identity_repair_api;
/

declare
    l_errors number;
    l_placeholders number;
    l_module_name varchar2(255);
    procedure restore_previous is
    begin
        if :identity_repair_previous_spec is not null
           and :identity_repair_previous_body is not null then
            execute immediate :identity_repair_previous_spec;
            execute immediate :identity_repair_previous_body;
        else
            begin
                execute immediate 'drop package slic_zkt_identity_repair_api';
            exception
                when others then
                    if sqlcode <> -4043 then
                        raise;
                    end if;
            end;
        end if;
    end restore_previous;
begin
    select count(*) into l_errors
      from user_errors
     where name = 'SLIC_ZKT_IDENTITY_REPAIR_API';
    select count(*)
      into l_placeholders
      from user_source
     where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
       and type = 'PACKAGE BODY'
       and (
           upper(text) like '%REPLACE_WITH_ADD_API_USERNAME%'
           or upper(text) like '%REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX%'
       );
    if l_errors <> 0 or l_placeholders <> 0 then
        restore_previous;
        raise_application_error(
            -20520,
            'Identity repair package validation failed and the previous package was restored.'
        );
    end if;

    begin
        select name into l_module_name
          from user_ords_modules
         where uri_prefix = '/raw_attn_capture_event/'
           and rownum = 1;
    exception
        when no_data_found then
            raise_application_error(-20521, 'The existing raw attendance ORDS module is missing.');
    end;

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/identity-repairs/capabilities');
    exception when dup_val_on_index then null;
    end;
    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/identity-repairs/capabilities',
        p_method => 'GET',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => 'begin slic_zkt_identity_repair_api.get_capabilities; end;');

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/identity-repairs/check');
    exception when dup_val_on_index then null;
    end;
    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/identity-repairs/check',
        p_method => 'POST',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => 'begin slic_zkt_identity_repair_api.post_check(:body_text); end;');

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/identity-repairs');
    exception when dup_val_on_index then null;
    end;
    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/identity-repairs',
        p_method => 'POST',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => 'begin slic_zkt_identity_repair_api.post_repair(:body_text); end;');

    begin
        ords.define_template(
            p_module_name => l_module_name,
            p_pattern => 'raw-captures/identity-repairs/status');
    exception when dup_val_on_index then null;
    end;
    ords.define_handler(
        p_module_name => l_module_name,
        p_pattern => 'raw-captures/identity-repairs/status',
        p_method => 'POST',
        p_source_type => ords.source_type_plsql,
        p_items_per_page => 0,
        p_source => 'begin slic_zkt_identity_repair_api.post_status(:body_text); end;');
    commit;
exception
    when others then
        rollback;
        begin
            restore_previous;
        exception
            when others then
                raise_application_error(
                    -20522,
                    'Identity repair deployment failed and automatic package restoration also failed.'
                );
        end;
        raise;
end;
/
