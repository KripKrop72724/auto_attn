set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Extend only the protected, read-only content verifier for a narrowly shaped
  legacy four-character UID corruption. It can confirm an existing Oracle row
  after exact source and full identity checks; it cannot create attendance.
  The ordinary 64-hex verifier and all delivery/repair handlers stay unchanged.
  This guarded patch restores the exact installed package body on failure.
*/

declare
    l_previous_body clob;
    l_normalized_body clob;
    l_candidate_body clob;
    l_replace_attempted boolean := false;
    l_status varchar2(30);
    l_errors number;
    l_old_1 constant varchar2(4000) := q'~        l_stored_serial hr_raw_attn_capture_events.device_serial%type;
        l_json clob;
~';
    l_new_1 constant varchar2(4000) := q'~        l_stored_serial hr_raw_attn_capture_events.device_serial%type;
        l_original_uid hr_raw_attn_capture_events.event_uid%type;
        l_legacy boolean;
        l_json clob;
        function legacy_uid_shape(p_uid in varchar2) return boolean is
            l_first pls_integer := 0;
            l_last pls_integer := 0;
            l_bad pls_integer := 0;
            l_char varchar2(1);
        begin
            if p_uid is null or length(p_uid) <> 64 then return false; end if;
            for i in 1..64 loop
                l_char := substr(p_uid, i, 1);
                if instr('0123456789abcdef', l_char) = 0 then
                    if l_char not in ('?', chr(8), chr(20)) then return false; end if;
                    if l_first = 0 then l_first := i; end if;
                    l_last := i;
                    l_bad := l_bad + 1;
                end if;
            end loop;
            return l_bad between 3 and 4 and l_first >= 25
                and l_last <= 36 and l_last - l_first <= 3;
        end legacy_uid_shape;

        function matches_original(p_legacy in varchar2, p_original in varchar2)
            return boolean is
            l_first pls_integer := 0;
            l_last pls_integer := 0;
            l_differences pls_integer := 0;
            l_bad pls_integer := 0;
            l_char varchar2(1);
        begin
            if not legacy_uid_shape(p_legacy)
               or not regexp_like(p_original, '^[0-9a-f]{64}$', 'c') then
                return false;
            end if;
            for i in 1..64 loop
                l_char := substr(p_legacy, i, 1);
                if l_char <> substr(p_original, i, 1) then
                    if l_first = 0 then l_first := i; end if;
                    l_last := i;
                    l_differences := l_differences + 1;
                    if l_char in ('?', chr(8), chr(20)) then l_bad := l_bad + 1; end if;
                end if;
            end loop;
            return l_differences = 4 and l_first >= 25 and l_last <= 36
                and l_last - l_first = 3 and l_bad >= 3;
        end matches_original;
~';
    l_old_2 constant varchar2(4000) := q'~            if not regexp_like(item.event_uid, '^[0-9a-f]{64}$', 'c')
               or item.device_serial is null
~';
    l_new_2 constant varchar2(4000) := q'~            l_legacy := item.event_uid is null or not regexp_like(item.event_uid, '^[0-9a-f]{64}$', 'c');
            if item.event_uid is null
               or (l_legacy and not legacy_uid_shape(item.event_uid))
               or item.device_serial is null
~';
    l_old_3 constant varchar2(4000) := q'~            select count(*) into l_count
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
~';
    l_new_3 constant varchar2(4000) := q'~            l_original_uid := null;
            l_token := null;
            if l_legacy then
                -- A damaged local ID can only confirm an existing Oracle row.
                -- It can never authorize inserting another copy of the punch.
                select count(*), min(event_uid) into l_count, l_original_uid
                  from hr_raw_attn_capture_events
                 where event_uid like substr(item.event_uid, 1, 24) || '%'
                   and device_serial = item.device_serial
                   and user_id = item.user_id
                   and event_timestamp = slic_zkt_truth_api.parse_event_timestamp(item.event_timestamp)
                   and raw_punch = item.raw_punch;
                if l_count = 0 then
                    l_classification := 'LEGACY_SOURCE_MISSING';
                elsif l_count > 1 then
                    l_classification := 'LEGACY_SOURCE_AMBIGUOUS';
                elsif not matches_original(item.event_uid, l_original_uid) then
                    l_classification := 'LEGACY_SOURCE_CONFLICT';
                else
                    select employee_name, cnic into l_stored_name, l_stored_cnic
                      from hr_raw_attn_capture_events
                     where event_uid = l_original_uid;
                    if nvl(l_stored_name, chr(0)) = nvl(item.employee_name, chr(0))
                       and l_stored_cnic = item.cnic then
                        l_classification := 'LEGACY_SOURCE_MATCH';
                        l_token := content_token(l_original_uid);
                    else
                        l_classification := 'LEGACY_SOURCE_CONFLICT';
                    end if;
                end if;
            else
                select count(*) into l_count
                  from hr_raw_attn_capture_events
                 where event_uid = item.event_uid;
                if l_count = 0 then
                    l_classification := 'MISSING';
                    l_token := content_token(item.event_uid);
                elsif l_count > 1 then
                    l_classification := 'CROSS_DEVICE_UID_COLLISION';
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
            end if;
~';
    l_old_4 constant varchar2(4000) := q'~            if l_classification in ('MATCH','MISSING','MISMATCH') then
                l_result.put('current_content_token', l_token);
            end if;
~';
    l_new_4 constant varchar2(4000) := q'~            if l_classification in ('MATCH','MISSING','MISMATCH','LEGACY_SOURCE_MATCH') then
                l_result.put('current_content_token', l_token);
            end if;
            if l_classification = 'LEGACY_SOURCE_MATCH' then
                l_result.put('matched_event_uid', l_original_uid);
            end if;
~';

    function occurrence_count(p_source in clob, p_marker in varchar2)
        return pls_integer is
        l_count pls_integer := 0;
        l_offset pls_integer := 1;
        l_found pls_integer;
    begin
        loop
            l_found := dbms_lob.instr(p_source, p_marker, l_offset);
            exit when l_found = 0;
            l_count := l_count + 1;
            l_offset := l_found + length(p_marker);
        end loop;
        return l_count;
    end occurrence_count;

    procedure validate_body is
    begin
        select status into l_status from user_objects
         where object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and object_type = 'PACKAGE BODY';
        select count(*) into l_errors from user_errors
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(-20891, 'Identity-repair package validation failed.');
        end if;
    end validate_body;
begin
    dbms_lob.createtemporary(l_previous_body, true);
    dbms_lob.createtemporary(l_normalized_body, true);
    dbms_lob.writeappend(l_previous_body, length('create or replace '), 'create or replace ');
    dbms_lob.writeappend(l_normalized_body, length('create or replace '), 'create or replace ');
    for source_line in (
        select text from user_source
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(l_previous_body, length(source_line.text), source_line.text);
        dbms_lob.writeappend(
            l_normalized_body,
            length(rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)),
            rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)
        );
    end loop;
    validate_body;
    if occurrence_count(l_normalized_body, l_new_1) = 1
       and occurrence_count(l_normalized_body, l_old_1) = 0
       and occurrence_count(l_normalized_body, l_new_2) = 1
       and occurrence_count(l_normalized_body, l_old_2) = 0
       and occurrence_count(l_normalized_body, l_new_3) = 1
       and occurrence_count(l_normalized_body, l_old_3) = 0
       and occurrence_count(l_normalized_body, l_new_4) = 1
       and occurrence_count(l_normalized_body, l_old_4) = 0 then
        dbms_output.put_line('legacy_source_check=already_installed');
        return;
    end if;
    if occurrence_count(l_normalized_body, l_old_1) <> 1
       or occurrence_count(l_normalized_body, l_new_1) <> 0
       or occurrence_count(l_normalized_body, l_old_2) <> 1
       or occurrence_count(l_normalized_body, l_new_2) <> 0
       or occurrence_count(l_normalized_body, l_old_3) <> 1
       or occurrence_count(l_normalized_body, l_new_3) <> 0
       or occurrence_count(l_normalized_body, l_old_4) <> 1
       or occurrence_count(l_normalized_body, l_new_4) <> 0
       or occurrence_count(l_normalized_body,
           q'~               or not regexp_like(item.event_uid, '^[0-9a-f]{64}$', 'c')~') <> 1 then
        raise_application_error(-20890, 'Installed package does not match the guarded verifier patch.');
    end if;
    l_candidate_body := l_normalized_body;
    l_candidate_body := replace(l_candidate_body, l_old_1, l_new_1);
    l_candidate_body := replace(l_candidate_body, l_old_2, l_new_2);
    l_candidate_body := replace(l_candidate_body, l_old_3, l_new_3);
    l_candidate_body := replace(l_candidate_body, l_old_4, l_new_4);
    if occurrence_count(l_candidate_body, l_new_1) <> 1
       or occurrence_count(l_candidate_body, l_new_2) <> 1
       or occurrence_count(l_candidate_body, l_new_3) <> 1
       or occurrence_count(l_candidate_body, l_new_4) <> 1 then
        raise_application_error(-20891, 'Legacy source-check markers are incomplete.');
    end if;
    l_replace_attempted := true;
    execute immediate l_candidate_body;
    validate_body;
    dbms_output.put_line('legacy_source_check=installed');
    dbms_output.put_line('attendance_rows_changed_by_migration=0');
exception
    when others then
        if l_replace_attempted and l_previous_body is not null then
            begin
                execute immediate l_previous_body;
                validate_body;
            exception
                when others then
                    raise_application_error(-20892, 'Verifier install and automatic restoration both failed.');
            end;
        end if;
        raise;
end;
/
