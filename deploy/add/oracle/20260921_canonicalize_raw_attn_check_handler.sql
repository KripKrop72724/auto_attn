set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Replace the legacy inline membership-check handler with the checked-in
  package-backed implementation.

  This changes ORDS handler metadata only.  It performs no attendance-table
  DML, removes the legacy inline credential verifier from the handler, and
  restores the exact previous handler automatically if replacement or
  verification fails.
*/
declare
    l_original_source          clob;
    l_original_source_type     varchar2(255);
    l_original_items_per_page  number;
    l_original_mimes_allowed   varchar2(4000);
    l_original_comments        varchar2(4000);
    l_installed_count          number;
    l_change_attempted         boolean := false;
    l_package_status           varchar2(30);
    l_package_errors           number;

    c_module_name constant varchar2(255) := 'raw_attendance_capture';
    c_pattern constant varchar2(255) := 'raw-captures/check';
    c_method constant varchar2(10) := 'POST';
    c_canonical_source constant varchar2(4000) :=
        'begin' || chr(10)
        || '    slic_zkt_truth_api.post_check(:body_text);' || chr(10)
        || 'end;';

    procedure restore_original is
    begin
        ords.define_handler(
            p_module_name => c_module_name,
            p_pattern => c_pattern,
            p_method => c_method,
            p_source_type => l_original_source_type,
            p_source => l_original_source,
            p_items_per_page => l_original_items_per_page,
            p_mimes_allowed => l_original_mimes_allowed,
            p_comments => l_original_comments);
        commit;
        l_change_attempted := false;
    end restore_original;
begin
    select status
      into l_package_status
      from user_objects
     where object_name = 'SLIC_ZKT_TRUTH_API'
       and object_type = 'PACKAGE BODY';

    select count(*)
      into l_package_errors
      from user_errors
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY';

    if l_package_status <> 'VALID' or l_package_errors <> 0 then
        raise_application_error(-20991, 'SLIC_ZKT_TRUTH_API is not valid.');
    end if;

    select h.source,
           h.source_type,
           h.items_per_page,
           h.mimes_allowed,
           h.comments
      into l_original_source,
           l_original_source_type,
           l_original_items_per_page,
           l_original_mimes_allowed,
           l_original_comments
      from ords_metadata.ords_modules m
      join ords_metadata.ords_templates t
        on t.module_id = m.id
      join ords_metadata.ords_handlers h
        on h.template_id = t.id
     where m.name = c_module_name
       and t.uri_template = c_pattern
       and h.method = c_method;

    if dbms_lob.compare(l_original_source, to_clob(c_canonical_source)) = 0 then
        dbms_output.put_line('raw-captures/check already uses the canonical package-backed handler.');
        return;
    end if;

    if dbms_lob.getlength(l_original_source) < 1000
       or dbms_lob.instr(upper(l_original_source), to_clob('HR_RAW_ATTN_CAPTURE_EVENTS')) = 0
       or dbms_lob.instr(upper(l_original_source), to_clob('JSON_TABLE')) = 0
       or dbms_lob.instr(upper(l_original_source), to_clob('SLIC_ZKT_TRUTH_API.POST_CHECK')) <> 0 then
        raise_application_error(
            -20992,
            'raw-captures/check was not the expected legacy inline membership handler.'
        );
    end if;

    l_change_attempted := true;
    ords.define_handler(
        p_module_name => c_module_name,
        p_pattern => c_pattern,
        p_method => c_method,
        p_source_type => l_original_source_type,
        p_source => to_clob(c_canonical_source),
        p_items_per_page => l_original_items_per_page,
        p_mimes_allowed => l_original_mimes_allowed,
        p_comments => l_original_comments);
    commit;

    select count(*)
      into l_installed_count
      from ords_metadata.ords_modules m
      join ords_metadata.ords_templates t
        on t.module_id = m.id
      join ords_metadata.ords_handlers h
        on h.template_id = t.id
     where m.name = c_module_name
       and t.uri_template = c_pattern
       and h.method = c_method
       and dbms_lob.compare(h.source, to_clob(c_canonical_source)) = 0;

    if l_installed_count <> 1 then
        restore_original;
        raise_application_error(
            -20993,
            'Canonical membership handler verification failed and the original was restored.'
        );
    end if;

    l_change_attempted := false;
    dbms_output.put_line(
        'raw-captures/check now uses SLIC_ZKT_TRUTH_API.post_check; attendance_rows_changed=0'
    );
exception
    when others then
        if l_change_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20994,
                        'Membership handler migration failed and automatic restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
