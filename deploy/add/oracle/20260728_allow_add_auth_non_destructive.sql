set define off
set serveroutput on

/*
  Production compatibility repair for ADD attendance delivery.

  Before execution, replace only:
    REPLACE_WITH_ADD_API_USERNAME
    REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX

  The script:
  - requires the current valid dual-verifier package shape;
  - preserves the primary and fleet verifiers byte-for-byte;
  - adds the approved ADD credential as a third SHA-256-only verifier;
  - requires both legacy reconcile DELETE statements to remain gated;
  - changes no table data itself;
  - validates the compiled package body; and
  - restores the exact original package body automatically on any failure.

  Re-running the same substituted script is safe. It validates the already
  installed verifier and exits without replacing the package body.

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
    l_add_declaration_count number;
    l_add_auth_count        number;
    l_fleet_auth_position   number;
    l_fleet_auth_next       number;
    l_fleet_closing_position number;
    l_fleet_tail_length     number;
    l_fleet_auth_tail       varchar2(4000);
    l_add_auth_clause       varchar2(4000);

    c_add_api_username constant varchar2(128) :=
        'REPLACE_WITH_ADD_API_USERNAME';
    c_add_api_password_sha256 constant varchar2(64) :=
        'REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX';

    c_fleet_password_pattern constant varchar2(4000) :=
        q'~(c_fleet_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';)~';
    c_fleet_auth_anchor constant varchar2(4000) :=
        'and v_password_digest = c_fleet_api_password_sha256';
    c_gated_delete_pattern constant varchar2(4000) :=
        q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;
begin
    if regexp_like(c_add_api_username, '^REPLACE_WITH_')
       or length(c_add_api_username) > 128
       or not regexp_like(
           c_add_api_password_sha256,
           '^[0-9A-F]{64}$',
           'c'
       ) then
        raise_application_error(
            -20201,
            'ADD credential placeholders were not replaced safely.'
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
            -20202,
            'SLIC_ZKT_TRUTH_API package body was not found.'
        );
    end if;

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

    l_fleet_auth_position := dbms_lob.instr(
        l_body,
        to_clob(c_fleet_auth_anchor)
    );
    l_fleet_auth_next := dbms_lob.instr(
        l_body,
        to_clob(c_fleet_auth_anchor),
        l_fleet_auth_position + 1
    );
    l_fleet_closing_position := dbms_lob.instr(
        l_body,
        to_clob(')'),
        l_fleet_auth_position + length(c_fleet_auth_anchor)
    );
    l_fleet_tail_length :=
        l_fleet_closing_position
        - (l_fleet_auth_position + length(c_fleet_auth_anchor));

    if l_body_status <> 'VALID'
       or l_compile_errors <> 0
       or regexp_count(l_body, c_gated_delete_pattern, 1, 'i') <> 2
       or regexp_count(l_body, c_fleet_password_pattern, 1, 'c') <> 1
       or l_fleet_auth_position = 0
       or l_fleet_auth_next <> 0
       or l_fleet_closing_position = 0
       or l_fleet_tail_length < 0
       or (
           l_fleet_tail_length > 0
           and not regexp_like(
               dbms_lob.substr(
                   l_body,
                   l_fleet_tail_length,
                   l_fleet_auth_position + length(c_fleet_auth_anchor)
               ),
               '^[[:space:]]+$'
           )
       )
       or regexp_count(
           l_body,
           'nvl\(v_username, chr\(0\)\) = c_fleet_api_username',
           1,
           'i'
       ) <> 1
       or regexp_count(
           l_body,
           'v_password_digest = c_api_password_sha256',
           1,
           'i'
       ) <> 1
       or regexp_count(
           l_body,
           q'~c_api_password constant varchar2~',
           1,
           'i'
       ) <> 0
       or regexp_count(
           l_body,
           q'~c_fleet_api_password constant varchar2~',
           1,
           'i'
       ) <> 0 then
        raise_application_error(
            -20203,
            'Deployed package failed the valid dual-verifier/non-destructive precondition.'
        );
    end if;

    l_add_declaration_count := regexp_count(
        l_body,
        q'~c_add_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';~',
        1,
        'c'
    );
    l_add_auth_count := regexp_count(
        l_body,
        'v_password_digest = c_add_api_password_sha256',
        1,
        'i'
    );

    if l_add_declaration_count <> 0 or l_add_auth_count <> 0 then
        if l_add_declaration_count <> 1
           or l_add_auth_count <> 1
           or dbms_lob.instr(
               l_body,
               to_clob(
                   'c_add_api_username constant varchar2(128) := '''
                   || replace(c_add_api_username, '''', '''''')
                   || ''';'
               )
           ) = 0
           or dbms_lob.instr(
               l_body,
               to_clob(
                   'c_add_api_password_sha256 constant varchar2(64) := '''
                   || c_add_api_password_sha256
                   || ''';'
               )
           ) = 0 then
            raise_application_error(
                -20204,
                'A different or malformed ADD verifier is already installed.'
            );
        end if;

        dbms_output.put_line(
            'Approved ADD verifier is already installed; package body is VALID and DELETE paths remain gated.'
        );
        return;
    end if;

    l_patched_body := regexp_replace(
        l_body,
        c_fleet_password_pattern,
        '\1'
            || chr(10)
            || '    c_add_api_username constant varchar2(128) := '''
            || replace(c_add_api_username, '''', '''''')
            || ''';'
            || chr(10)
            || '    c_add_api_password_sha256 constant varchar2(64) := '''
            || c_add_api_password_sha256
            || ''';',
        1,
        1,
        'c'
    );
    l_fleet_auth_position := dbms_lob.instr(
        l_patched_body,
        to_clob(c_fleet_auth_anchor)
    );
    l_fleet_closing_position := dbms_lob.instr(
        l_patched_body,
        to_clob(')'),
        l_fleet_auth_position + length(c_fleet_auth_anchor)
    );
    l_fleet_auth_tail := dbms_lob.substr(
        l_patched_body,
        l_fleet_closing_position - l_fleet_auth_position + 1,
        l_fleet_auth_position
    );
    l_add_auth_clause :=
        chr(10)
            || '            or ('
            || chr(10)
            || '                nvl(v_username, chr(0)) = c_add_api_username'
            || chr(10)
            || '                and v_password_digest = c_add_api_password_sha256'
            || chr(10)
            || '            )';
    l_patched_body := replace(
        l_patched_body,
        l_fleet_auth_tail,
        l_fleet_auth_tail || l_add_auth_clause
    );

    if regexp_count(
           l_patched_body,
           q'~c_add_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';~',
           1,
           'c'
       ) <> 1
       or regexp_count(
           l_patched_body,
           'v_password_digest = c_add_api_password_sha256',
           1,
           'i'
       ) <> 1
       or regexp_count(
           l_patched_body,
           c_gated_delete_pattern,
           1,
           'i'
       ) <> 2
       or regexp_count(
           l_patched_body,
           q'~c_add_api_password constant varchar2~',
           1,
           'i'
       ) <> 0 then
        raise_application_error(
            -20205,
            'Patched package failed the triple-verifier/non-destructive invariant'
                || ' (add_declarations='
                || regexp_count(
                    l_patched_body,
                    q'~c_add_api_password_sha256 constant varchar2\(64\) := '[0-9A-F]{64}';~',
                    1,
                    'c'
                )
                || ', add_auth_refs='
                || regexp_count(
                    l_patched_body,
                    'v_password_digest = c_add_api_password_sha256',
                    1,
                    'i'
                )
                || ', delete_gates='
                || regexp_count(
                    l_patched_body,
                    c_gated_delete_pattern,
                    1,
                    'i'
                )
                || ', plaintext_add_constants='
                || regexp_count(
                    l_patched_body,
                    q'~c_add_api_password constant varchar2~',
                    1,
                    'i'
                )
                || ').'
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
            -20206,
            'Patched package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'SLIC_ZKT_TRUTH_API now accepts the approved ADD verifier; existing verifiers are preserved; DELETE paths remain gated; package body is VALID.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20207,
                        'Migration failed and automatic package restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
