set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Correct the downstream status proof-order guard.

  SLIC_ZKT_DOWNSTREAM_REPAIR writes its exact operation-ID proof before the
  caller writes the matching repair receipt, and both rows commit atomically.
  Requiring that proof's timestamp to be on or after the later receipt timestamp
  therefore rejects every valid transaction.  The exact operation-ID join,
  unique keys, and atomic commit are the durable freshness boundary.

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
    l_old_guard constant varchar2(500) := q'~                        and l_downstream_observed_at is not null
                        and l_downstream_observed_at >= receipt.created_at);~';
    l_new_guard constant varchar2(500) := q'~                        and l_downstream_observed_at is not null);~';

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
                -20861,
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

    if occurrence_count(l_normalized_body, l_new_guard) = 2
       and occurrence_count(l_normalized_body, l_old_guard) = 0 then
        validate_body;
        dbms_output.put_line('identity_repair_status_receipt_order=already_fixed');
        return;
    end if;
    if occurrence_count(l_normalized_body, l_old_guard) <> 2
       or occurrence_count(l_normalized_body, l_new_guard) <> 0 then
        raise_application_error(
            -20860,
            'Installed identity-repair body does not match the guarded receipt-order patch shape.'
        );
    end if;

    l_candidate_body := replace(
        l_normalized_body,
        l_old_guard,
        l_new_guard
    );
    execute immediate l_candidate_body;
    validate_body;
    if occurrence_count(l_candidate_body, l_new_guard) <> 2
       or occurrence_count(l_candidate_body, l_old_guard) <> 0 then
        raise_application_error(
            -20861,
            'Identity-repair receipt-order markers are incomplete.'
        );
    end if;
    dbms_output.put_line('identity_repair_status_receipt_order=fixed');
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
                        -20862,
                        'Identity-repair receipt-order fix and automatic restoration both failed.'
                    );
            end;
        end if;
        raise;
end;
/
