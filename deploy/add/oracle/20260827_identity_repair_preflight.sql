set define off
set serveroutput on

/*
  Read-only production inventory for employee attendance identity repair.

  Run and retain the spool before deploying the contract.  This script fails
  on duplicate event UIDs, a missing unique UID index, missing immutable/raw
  columns, or an invalid daily flag helper.  The downstream adapter is reported
  separately: preview/check may deploy without it, but correction capability
  remains false and ADD execution must stay disabled.
*/
declare
    l_missing_columns number;
    l_duplicate_uids number;
    l_unique_uid_indexes number;
    l_helper_valid number;
    l_adapter_valid number;
    l_status_view_valid number;
    l_database_charset varchar2(128);
begin
    select value
      into l_database_charset
      from nls_database_parameters
     where parameter = 'NLS_CHARACTERSET';
    select count(*)
      into l_missing_columns
      from (
          select column_value column_name
            from table(sys.odcivarchar2list(
                'EVENT_UID', 'ZONE_ID', 'DEVICE_ID', 'DEVICE_SERIAL',
                'USER_ID', 'EMPLOYEE_NAME', 'CNIC', 'EVENT_TIMESTAMP',
                'ATTENDANCE_DATE', 'CHECK_IN', 'CHECK_OUT', 'RAW_PUNCH',
                'DATASYNC'
            ))
      ) required
     where not exists (
               select 1
                 from user_tab_columns stored
                where stored.table_name = 'HR_RAW_ATTN_CAPTURE_EVENTS'
                  and stored.column_name = required.column_name
           );

    select count(*)
      into l_duplicate_uids
      from (
          select event_uid
            from hr_raw_attn_capture_events
           group by event_uid
          having count(*) > 1
      );

    select count(*)
      into l_unique_uid_indexes
      from user_indexes indexes
     where indexes.table_name = 'HR_RAW_ATTN_CAPTURE_EVENTS'
       and indexes.uniqueness = 'UNIQUE'
       and exists (
               select 1
                 from user_ind_columns columns
                where columns.index_name = indexes.index_name
                  and columns.table_name = indexes.table_name
                  and columns.column_name = 'EVENT_UID'
                  and columns.column_position = 1
           )
       and 1 = (
               select count(*)
                 from user_ind_columns columns
                where columns.index_name = indexes.index_name
                  and columns.table_name = indexes.table_name
           );

    select count(*)
      into l_helper_valid
      from user_objects
     where object_name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
       and object_type = 'PROCEDURE'
       and status = 'VALID';

    select count(*)
      into l_adapter_valid
      from user_objects
     where object_name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
       and object_type = 'PACKAGE BODY'
       and status = 'VALID';

    select count(*)
      into l_status_view_valid
      from user_objects
     where object_name = 'SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS'
       and object_type = 'VIEW'
       and status = 'VALID';

    dbms_output.put_line('missing_required_columns=' || l_missing_columns);
    dbms_output.put_line('duplicate_event_uid_groups=' || l_duplicate_uids);
    dbms_output.put_line('single_column_unique_uid_indexes=' || l_unique_uid_indexes);
    dbms_output.put_line('daily_flag_helper_valid=' || l_helper_valid);
    dbms_output.put_line('downstream_adapter_valid=' || l_adapter_valid);
    dbms_output.put_line('downstream_status_view_valid=' || l_status_view_valid);
    dbms_output.put_line('database_charset=' || l_database_charset);

    dbms_output.put_line('--- objects referencing DATASYNC or raw attendance ---');
    for dependency in (
        select distinct name, type
          from user_source
         where upper(text) like '%DATASYNC%'
            or upper(text) like '%HR_RAW_ATTN_CAPTURE_EVENTS%'
         order by type, name
    ) loop
        dbms_output.put_line(dependency.type || ':' || dependency.name);
    end loop;

    dbms_output.put_line('--- enabled scheduler jobs ---');
    for job in (
        select job_name, job_type, enabled
          from user_scheduler_jobs
         where enabled = 'TRUE'
         order by job_name
    ) loop
        dbms_output.put_line(job.job_name || ':' || job.job_type || ':' || job.enabled);
    end loop;

    if l_missing_columns <> 0 then
        raise_application_error(-20501, 'Raw attendance table shape is not repair-compatible.');
    end if;
    if l_duplicate_uids <> 0 then
        raise_application_error(-20502, 'Duplicate event UIDs block identity repair deployment.');
    end if;
    if l_unique_uid_indexes = 0 then
        raise_application_error(-20503, 'A single-column unique EVENT_UID index is required.');
    end if;
    if l_helper_valid <> 1 then
        raise_application_error(-20504, 'The daily check-in/check-out helper is missing or invalid.');
    end if;
    if l_database_charset <> 'AL32UTF8' then
        raise_application_error(
            -20505,
            'AL32UTF8 is required for byte-identical ADD/Oracle repair payload digests.'
        );
    end if;

    if l_adapter_valid = 1 and l_status_view_valid = 1 then
        dbms_output.put_line('EXECUTION_READY=TRUE');
    else
        dbms_output.put_line(
            'EXECUTION_READY=FALSE: inventory and implement the real DATASYNC consumer adapter.'
        );
    end if;
end;
/
