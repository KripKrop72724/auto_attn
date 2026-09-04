set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Let an identity-repair operation use the newest verified projection proof for
  the same employee/day during downstream status checks.

  A multi-punch repair projects an employee/day after every committed punch.
  The next punch on that day legitimately supersedes the prior day digest, so
  comparing every operation forever with only its own intermediate digest
  leaves earlier operations pending even when the final projection is exact.

  This migration changes only SLIC_ZKT_IDENTITY_REPAIR_API's package body.  It
  performs no table DML and restores the exact previous body automatically if
  compilation or validation fails.
*/

declare
    l_previous_body clob;
    l_normalized_body clob;
    l_candidate_body clob;
    l_status varchar2(30);
    l_errors number;
    l_old_status_query constant varchar2(32767) := q'^                            execute immediate
                                'select downstream_verified, stale_old_identity_absent, '
                                || 'identity_digest, observed_at '
                                || 'from slic_zkt_repair_downstream_status '
                                || 'where operation_id = :operation_id'
                                into l_downstream_verified, l_stale_absent,
                                     l_downstream_digest, l_downstream_observed_at
                                using receipt.operation_id;^';
    l_new_status_query constant varchar2(32767) := q'^                            execute immediate q'~
                                with requested_log as (
                                    select *
                                      from slic_zkt_ds_repair_log
                                     where operation_id = :operation_id
                                ), projection_versions as (
                                    select candidate.operation_id,
                                           candidate.new_employee_id employee_id,
                                           candidate.attendance_date,
                                           candidate.new_projection_digest projection_digest,
                                           candidate.downstream_verified verified,
                                           candidate.observed_at
                                      from slic_zkt_ds_repair_log candidate
                                      join requested_log requested
                                        on requested.attendance_date = candidate.attendance_date
                                       and (
                                           candidate.new_employee_id = requested.new_employee_id
                                           or candidate.new_employee_id = requested.old_employee_id
                                       )
                                    union all
                                    select candidate.operation_id,
                                           candidate.old_employee_id employee_id,
                                           candidate.attendance_date,
                                           candidate.old_projection_digest projection_digest,
                                           candidate.stale_old_identity_absent verified,
                                           candidate.observed_at
                                      from slic_zkt_ds_repair_log candidate
                                      join requested_log requested
                                        on requested.attendance_date = candidate.attendance_date
                                       and (
                                           candidate.old_employee_id = requested.new_employee_id
                                           or candidate.old_employee_id = requested.old_employee_id
                                       )
                                     where candidate.old_employee_id is not null
                                       and candidate.old_employee_id <> candidate.new_employee_id
                                ), latest_projections as (
                                    select projection_versions.*,
                                           row_number() over (
                                               partition by employee_id, attendance_date
                                               order by observed_at desc, operation_id desc
                                           ) projection_rank
                                      from projection_versions
                                )
                                select case
                                           when current_log.downstream_verified = 'T'
                                            and latest_new.verified = 'T'
                                            and slic_zkt_downstream_repair.projection_digest(
                                                    current_log.new_employee_id,
                                                    current_log.attendance_date
                                                ) = latest_new.projection_digest
                                           then 'T' else 'F'
                                       end,
                                       case
                                           when current_log.stale_old_identity_absent = 'T'
                                            and (
                                                current_log.old_employee_id is null
                                                or current_log.old_employee_id = current_log.new_employee_id
                                                or (
                                                    latest_old.verified = 'T'
                                                    and slic_zkt_downstream_repair.projection_digest(
                                                            current_log.old_employee_id,
                                                            current_log.attendance_date
                                                        ) = latest_old.projection_digest
                                                )
                                            )
                                           then 'T' else 'F'
                                       end,
                                       receipt.desired_identity_digest,
                                       greatest(
                                           current_log.observed_at,
                                           latest_new.observed_at,
                                           case
                                               when current_log.old_employee_id is null
                                                 or current_log.old_employee_id = current_log.new_employee_id
                                               then current_log.observed_at
                                               else latest_old.observed_at
                                           end
                                       )
                                  from requested_log current_log
                                  join slic_zkt_id_repair_receipts receipt
                                    on receipt.operation_id = current_log.operation_id
                                  join latest_projections latest_new
                                    on latest_new.employee_id = current_log.new_employee_id
                                   and latest_new.attendance_date = current_log.attendance_date
                                   and latest_new.projection_rank = 1
                                  left join latest_projections latest_old
                                    on latest_old.employee_id = current_log.old_employee_id
                                   and latest_old.attendance_date = current_log.attendance_date
                                   and latest_old.projection_rank = 1
                            ~'
                                into l_downstream_verified, l_stale_absent,
                                     l_downstream_digest, l_downstream_observed_at
                                using receipt.operation_id;^';

    function occurrence_count(p_source in clob, p_marker in varchar2)
        return pls_integer
    is
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
        select status
          into l_status
          from user_objects
         where object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and object_type = 'PACKAGE BODY';
        select count(*)
          into l_errors
          from user_errors
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(
                -20871,
                'Identity-repair package body validation failed.'
            );
        end if;
    end validate_body;
begin
    dbms_lob.createtemporary(l_previous_body, true);
    dbms_lob.createtemporary(l_normalized_body, true);
    dbms_lob.writeappend(
        l_previous_body,
        length('create or replace '),
        'create or replace '
    );
    dbms_lob.writeappend(
        l_normalized_body,
        length('create or replace '),
        'create or replace '
    );
    for source_line in (
        select text
          from user_source
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(
            l_previous_body,
            length(source_line.text),
            source_line.text
        );
        dbms_lob.writeappend(
            l_normalized_body,
            length(rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)),
            rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)
        );
    end loop;

    if occurrence_count(l_normalized_body, l_new_status_query) = 1
       and occurrence_count(l_normalized_body, l_old_status_query) = 0 then
        validate_body;
        dbms_output.put_line('identity_repair_status_supersession=already_fixed');
        return;
    end if;
    if occurrence_count(l_normalized_body, l_old_status_query) <> 1
       or occurrence_count(l_normalized_body, l_new_status_query) <> 0 then
        raise_application_error(
            -20870,
            'Installed identity-repair body does not match the guarded patch shape.'
        );
    end if;

    l_candidate_body := replace(
        l_normalized_body,
        l_old_status_query,
        l_new_status_query
    );
    execute immediate l_candidate_body;
    validate_body;
    if occurrence_count(l_candidate_body, l_new_status_query) <> 1
       or occurrence_count(l_candidate_body, l_old_status_query) <> 0 then
        raise_application_error(
            -20871,
            'Identity-repair downstream-status markers are incomplete.'
        );
    end if;
    dbms_output.put_line('identity_repair_status_supersession=fixed');
    dbms_output.put_line('attendance_rows_changed=0');
exception
    when others then
        if l_previous_body is not null then
            begin
                execute immediate l_previous_body;
                validate_body;
            exception
                when others then
                    raise_application_error(
                        -20872,
                        'Identity-repair status fix and automatic restoration both failed.'
                    );
            end;
        end if;
        raise;
end;
/
