set define off
set serveroutput on

/*
  Replace only the legacy raw-captures POST handler with the canonical
  SLIC_ZKT_TRUTH_API.post_live wrapper.

  This migration:
  - changes ORDS handler metadata only;
  - performs no attendance table DML;
  - requires the valid triple-verifier, non-destructive package;
  - requires the known legacy inline handler shape before replacement;
  - preserves the original handler source and all handler properties;
  - restores that exact handler automatically on any failure; and
  - is idempotent once the canonical wrapper is installed.
*/

declare
    l_original_source          clob;
    l_original_source_type     varchar2(255);
    l_original_items_per_page  number;
    l_original_mimes_allowed   varchar2(4000);
    l_original_comments        varchar2(4000);
    l_body_status              varchar2(30);
    l_compile_errors           number;
    l_add_auth_refs            number;
    l_delete_gate_lines        number;
    l_installed_count          number;
    l_change_attempted         boolean := false;

    c_module_name constant varchar2(255) := 'raw_attendance_capture';
    c_pattern constant varchar2(255) := 'raw-captures';
    c_method constant varchar2(10) := 'POST';
    c_canonical_source constant varchar2(4000) :=
        'begin' || chr(10)
        || '    slic_zkt_truth_api.post_live(:body_text);' || chr(10)
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
      into l_body_status
      from user_objects
     where object_name = 'SLIC_ZKT_TRUTH_API'
       and object_type = 'PACKAGE BODY';

    select count(*)
      into l_compile_errors
      from user_errors
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY';

    select count(*)
      into l_add_auth_refs
      from user_source
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY'
       and upper(text) like '%V_PASSWORD_DIGEST = C_ADD_API_PASSWORD_SHA256%';

    select count(*)
      into l_delete_gate_lines
      from user_source
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY'
       and upper(text) like '%WHERE 1 = 0%';

    if l_body_status <> 'VALID'
       or l_compile_errors <> 0
       or l_add_auth_refs <> 1
       or l_delete_gate_lines <> 2 then
        raise_application_error(
            -20301,
            'Canonical package is not valid, triple-verifier, and non-destructive.'
        );
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

    if dbms_lob.compare(
           l_original_source,
           to_clob(c_canonical_source)
       ) = 0 then
        dbms_output.put_line(
            'raw-captures already uses the canonical package-backed live handler.'
        );
        return;
    end if;

    if dbms_lob.getlength(l_original_source) < 1000
       or dbms_lob.instr(
           upper(l_original_source),
           to_clob('INSERT INTO HR_RAW_ATTN_CAPTURE_EVENTS')
       ) = 0
       or dbms_lob.instr(
           upper(l_original_source),
           to_clob('DELETE FROM HR_RAW_ATTN_CAPTURE_EVENTS')
       ) = 0
       or dbms_lob.instr(
           upper(l_original_source),
           to_clob('STANDARD_HASH')
       ) = 0
       or dbms_lob.instr(
           upper(l_original_source),
           to_clob('SLIC_ZKT_TRUTH_API.POST_LIVE')
       ) <> 0 then
        raise_application_error(
            -20302,
            'raw-captures handler was not the expected legacy inline implementation.'
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
       and h.source_type = l_original_source_type
       and dbms_lob.compare(
           h.source,
           to_clob(c_canonical_source)
       ) = 0;

    if l_installed_count <> 1 then
        restore_original;
        raise_application_error(
            -20303,
            'Canonical handler verification failed and the original was restored.'
        );
    end if;

    l_change_attempted := false;
    dbms_output.put_line(
        'raw-captures now uses SLIC_ZKT_TRUTH_API.post_live; no attendance table DML was run by this migration.'
    );
exception
    when others then
        if l_change_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20304,
                        'Handler migration failed and automatic restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
