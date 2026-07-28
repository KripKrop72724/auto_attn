set define off
set serveroutput on

/*
  Production compatibility repair for already-provisioned Zone Lite devices.

  Before execution, replace only:
    REPLACE_WITH_FLEET_API_USERNAME
    REPLACE_WITH_FLEET_64_CHARACTER_SHA256_HEX

  The script:
  - preserves the current ADD/raw-capture verifier;
  - adds the existing fleet credential as a second SHA-256 verifier;
  - gates both legacy reconcile DELETE statements so no attendance row can be
    removed;
  - changes no table data itself;
  - validates the compiled package body; and
  - restores the exact original package body automatically on any failure.

  Never commit a production username, password, or password verifier.
*/

declare
    l_body                  clob;
    l_patched_body          clob;
    l_original_ddl          clob;
    l_patched_ddl           clob;
    l_body_status           varchar2(30);
    l_compile_errors        number;
    l_ddl_attempted         boolean := false;

    c_fleet_api_username constant varchar2(128) :=
        'REPLACE_WITH_FLEET_API_USERNAME';
    c_fleet_api_password_sha256 constant varchar2(64) :=
        'REPLACE_WITH_FLEET_64_CHARACTER_SHA256_HEX';

    c_primary_password_pattern constant varchar2(4000) :=
        q'~(c_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';)~';
    c_password_declaration_pattern constant varchar2(4000) :=
        q'~v_password varchar2\(1024\);~';
    c_single_auth_pattern constant varchar2(4000) :=
        q'~if nvl\(v_username, chr\(0\)\) <> c_api_username[[:space:]]+or password_sha256\([[:space:]]+nvl\(v_password, chr\(0\)\)[[:space:]]+\) <> c_api_password_sha256 then~';
    c_delete_pattern constant varchar2(4000) :=
        q'~(delete from hr_raw_attn_capture_events d[[:space:]]+where)~';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;
begin
    if regexp_like(c_fleet_api_username, '^REPLACE_WITH_')
       or length(c_fleet_api_username) > 128
       or not regexp_like(
           c_fleet_api_password_sha256,
           '^[0-9A-F]{64}$',
           'c'
       ) then
        raise_application_error(
            -20101,
            'Fleet credential placeholders were not replaced safely.'
        );
    end if;

    dbms_lob.createtemporary(l_body, true);
    for source_line in (
        select text
          from user_source
         where name = 'SLIC_ZKT_TRUTH_API'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(
            l_body,
            length(source_line.text),
            source_line.text
        );
    end loop;

    if dbms_lob.getlength(l_body) = 0 then
        raise_application_error(
            -20102,
            'SLIC_ZKT_TRUTH_API package body was not found.'
        );
    end if;

    if regexp_count(l_body, c_primary_password_pattern, 1, 'c') <> 1
       or regexp_count(l_body, c_password_declaration_pattern, 1, 'i') <> 1
       or regexp_count(l_body, c_single_auth_pattern, 1, 'i') <> 1
       or regexp_count(l_body, c_delete_pattern, 1, 'i') <> 2
       or regexp_count(
           l_body,
           'c_fleet_api_password_sha256',
           1,
           'i'
       ) <> 0
       or regexp_count(
           l_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 0 then
        raise_application_error(
            -20103,
            'Deployed package shape was not the expected single-verifier destructive version.'
        );
    end if;

    l_patched_body := regexp_replace(
        l_body,
        c_primary_password_pattern,
        '\1'
            || chr(10)
            || '    c_fleet_api_username constant varchar2(128) := '''
            || replace(c_fleet_api_username, '''', '''''')
            || ''';'
            || chr(10)
            || '    c_fleet_api_password_sha256 constant varchar2(64) := '''
            || c_fleet_api_password_sha256
            || ''';',
        1,
        1,
        'c'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_password_declaration_pattern,
        'v_password varchar2(1024);'
            || chr(10)
            || '        v_password_digest varchar2(64);',
        1,
        1,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_single_auth_pattern,
        'v_password_digest := password_sha256(nvl(v_password, chr(0)));'
            || chr(10)
            || '        if not ('
            || chr(10)
            || '            (nvl(v_username, chr(0)) = c_api_username'
            || chr(10)
            || '             and v_password_digest = c_api_password_sha256)'
            || chr(10)
            || '            or (nvl(v_username, chr(0)) = c_fleet_api_username'
            || chr(10)
            || '                and v_password_digest = c_fleet_api_password_sha256)'
            || chr(10)
            || '        ) then',
        1,
        1,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_delete_pattern,
        '\1 1 = 0'
            || chr(10)
            || '           and',
        1,
        0,
        'i'
    );

    if regexp_count(
           l_patched_body,
           q'~c_fleet_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';~',
           1,
           'c'
       ) <> 1
       or regexp_count(
           l_patched_body,
           'v_password_digest = c_fleet_api_password_sha256',
           1,
           'i'
       ) <> 1
       or regexp_count(
           l_patched_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 2
       or regexp_count(
           l_patched_body,
           q'~c_api_password constant varchar2~',
           1,
           'i'
       ) <> 0 then
        raise_application_error(
            -20104,
            'Patched package failed the dual-verifier/non-destructive invariant.'
        );
    end if;

    l_original_ddl := to_clob('create or replace ') || l_body;
    l_patched_ddl := to_clob('create or replace ') || l_patched_body;

    l_ddl_attempted := true;
    execute immediate l_patched_ddl;

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

    if l_body_status <> 'VALID' or l_compile_errors <> 0 then
        restore_original;
        raise_application_error(
            -20105,
            'Patched package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'SLIC_ZKT_TRUTH_API now accepts both hashed verifiers; DELETE paths are gated; package body is VALID.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20106,
                        'Migration failed and automatic package restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
