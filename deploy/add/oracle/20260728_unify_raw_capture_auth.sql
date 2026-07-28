set define off
set serveroutput on

/*
  One-time production repair for a legacy ORDS authentication split.

  The deployed raw-captures/check handler already contains the authorized
  credential verifier, while SLIC_ZKT_TRUTH_API may still contain a different
  legacy verifier used by raw-captures/reconcile. This migration:

  1. reads the already-working verifier inside Oracle;
  2. patches only the four expected authentication expressions in the
     deployed package body;
  3. stores the password as an uppercase SHA-256 digest, never plaintext;
  4. validates package compilation; and
  5. restores the original body automatically if replacement or compilation
     fails.

  No credential value is selected, printed, accepted as substitution input, or
  committed to this file.
*/

declare
    l_body                  clob;
    l_patched_body          clob;
    l_original_ddl          clob;
    l_patched_ddl           clob;
    l_check_handler         clob;
    l_check_prefix          varchar2(32767);
    l_api_username          varchar2(128);
    l_api_password          varchar2(1024);
    l_api_password_sha256   varchar2(64);
    l_body_status           varchar2(30);
    l_compile_errors        number;
    l_ddl_attempted         boolean := false;

    c_username_pattern constant varchar2(4000) :=
        q'~c_api_username constant varchar2\(128\) := '[^']*';~';
    c_password_pattern constant varchar2(4000) :=
        q'~c_api_password constant varchar2\(512\) := '[^']*';~';
    c_password_declaration_pattern constant varchar2(4000) :=
        q'~v_password varchar2\(1024\);~';
    c_password_check_pattern constant varchar2(4000) :=
        q'~or nvl\(v_password, chr\(0\)\) <> c_api_password then~';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;
begin
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
            -20001,
            'SLIC_ZKT_TRUTH_API package body was not found.'
        );
    end if;

    select h.source
      into l_check_handler
      from user_ords_modules m
      join user_ords_templates t
        on t.module_id = m.id
      join user_ords_handlers h
        on h.template_id = t.id
     where m.name = 'raw_attendance_capture'
       and t.uri_template = 'raw-captures/check'
       and h.method = 'POST';

    l_check_prefix := dbms_lob.substr(l_check_handler, 32767, 1);
    l_api_username := regexp_substr(
        l_check_prefix,
        q'~nvl\(u,\s*chr\(0\)\)\s*<>\s*'([^']*)'~',
        1,
        1,
        'in',
        1
    );
    l_api_password := regexp_substr(
        l_check_prefix,
        q'~nvl\(p,\s*chr\(0\)\)\s*<>\s*'([^']*)'~',
        1,
        1,
        'in',
        1
    );

    if l_api_username is null
       or l_api_password is null
       or length(l_api_username) > 128 then
        raise_application_error(
            -20002,
            'The working membership-check verifier could not be parsed safely.'
        );
    end if;

    if regexp_count(l_body, c_username_pattern, 1, 'i') <> 1
       or regexp_count(l_body, c_password_pattern, 1, 'i') <> 1
       or regexp_count(l_body, c_password_declaration_pattern, 1, 'i') <> 1
       or regexp_count(l_body, c_password_check_pattern, 1, 'i') <> 1 then
        raise_application_error(
            -20003,
            'Legacy package authentication shape was not exactly as expected.'
        );
    end if;

    select rawtohex(standard_hash(l_api_password, 'SHA256'))
      into l_api_password_sha256
      from dual;
    l_api_password := null;

    l_patched_body := regexp_replace(
        l_body,
        c_username_pattern,
        'c_api_username constant varchar2(128) := '''
            || replace(l_api_username, '''', '''''')
            || ''';',
        1,
        0,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_password_pattern,
        'c_api_password_sha256 constant varchar2(64) := '''
            || l_api_password_sha256
            || ''';',
        1,
        0,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_password_declaration_pattern,
        'v_password varchar2(1024);' || chr(10)
            || chr(10)
            || '        function password_sha256(p_value in varchar2) return varchar2 is'
            || chr(10)
            || '            v_digest varchar2(64);'
            || chr(10)
            || '        begin'
            || chr(10)
            || '            select rawtohex(standard_hash(p_value, ''SHA256''))'
            || chr(10)
            || '              into v_digest'
            || chr(10)
            || '              from dual;'
            || chr(10)
            || '            return v_digest;'
            || chr(10)
            || '        end password_sha256;',
        1,
        0,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_password_check_pattern,
        q'~or password_sha256(nvl(v_password, chr(0))) <> c_api_password_sha256 then~',
        1,
        0,
        'i'
    );

    if regexp_count(
           l_patched_body,
           q'~c_api_password constant varchar2\(512\)~',
           1,
           'i'
       ) <> 0
       or regexp_count(
           l_patched_body,
           q'~c_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';~',
           1,
           'c'
       ) <> 1 then
        raise_application_error(
            -20004,
            'The patched package did not pass the credential-storage invariant.'
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
            -20005,
            'Patched package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'SLIC_ZKT_TRUTH_API authentication unified; package body is VALID.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20006,
                        'Migration failed and automatic package restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
