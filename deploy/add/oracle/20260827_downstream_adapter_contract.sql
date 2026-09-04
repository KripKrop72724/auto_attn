set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Production downstream adapter for HR_RAW_ATTN_CAPTURE_EVENTS identity repair.

  Verified production chain:
    HR_RAW_ATTN_CAPTURE_EVENTS
      -> PR_SYNC_ATTN_FROM_RAW / TRG_RAW_ATTN_SYNC_BI
      -> HR_EMPLOYEE_ATTENDANCE

  Existing-table scope is intentionally narrow:
  - HR_RAW_ATTN_CAPTURE_EVENTS is read by this adapter after the repair API
    changes only effective identity and derived daily flags.
  - HR_EMPLOYEE is read only to resolve CNIC to EMPLOYEE_ID.
  - HR_EMPLOYEE_ATTENDANCE is the sole downstream table which may be repaired.
  - No leave, payroll, roster, employee-master, or other business row is changed.

  Installation is additive: it creates empty receipt/log tables, a package, an
  index, and a verification view. Installation calls no repair procedure and
  changes no attendance data.

  Runtime safety:
  - leave, payroll, override, and manually marked days fail closed;
  - only a MARKED_BY='BIOMETRIC' row can be removed after its last valid raw
    punch moves away;
  - unresolved historical attribution fails closed;
  - affected employee/day rows are locked in stable order;
  - exact downstream projections are verified before a PII-free receipt is
    written in the caller's transaction.
*/

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
    end if;

    select count(*) into l_exists
      from user_indexes
     where index_name = 'IX_SLIC_ZKT_ID_REPAIR_EVENT';
    if l_exists = 0 then
        execute immediate q'~
            create index ix_slic_zkt_id_repair_event
                on slic_zkt_id_repair_receipts (event_uid, created_at)
        ~';
    end if;

    select count(*) into l_exists
      from user_tables
     where table_name = 'SLIC_ZKT_DS_REPAIR_LOG';
    if l_exists = 0 then
        execute immediate q'~
            create table slic_zkt_ds_repair_log (
                operation_id varchar2(36) not null,
                event_uid varchar2(150) not null,
                attendance_date date not null,
                old_employee_id number,
                new_employee_id number not null,
                old_resolution varchar2(30) not null,
                old_projection_action varchar2(24) not null,
                new_projection_action varchar2(24) not null,
                old_projection_digest varchar2(64) not null,
                new_projection_digest varchar2(64) not null,
                downstream_verified char(1) default 'T' not null,
                stale_old_identity_absent char(1) default 'T' not null,
                observed_at timestamp with time zone default systimestamp not null,
                constraint pk_slic_zkt_ds_repair primary key (operation_id),
                constraint ck_slic_zkt_ds_resolution check (
                    old_resolution in (
                        'MISSING_RAW', 'SAME_EMPLOYEE', 'MAPPED_EMPLOYEE',
                        'UNMAPPED_UNSYNCED'
                    )
                ),
                constraint ck_slic_zkt_ds_action check (
                    old_projection_action in (
                        'NONE', 'SAME_EMPLOYEE', 'INSERTED', 'UPDATED',
                        'REMOVED', 'PRESERVED'
                    )
                    and new_projection_action in (
                        'NONE', 'SAME_EMPLOYEE', 'INSERTED', 'UPDATED',
                        'REMOVED', 'PRESERVED'
                    )
                ),
                constraint ck_slic_zkt_ds_verified check (
                    downstream_verified = 'T'
                    and stale_old_identity_absent = 'T'
                )
            )
        ~';
    end if;

    select count(*) into l_exists
      from user_indexes
     where index_name = 'IX_SLIC_ZKT_DS_REPAIR_EVENT';
    if l_exists = 0 then
        execute immediate q'~
            create index ix_slic_zkt_ds_repair_event
                on slic_zkt_ds_repair_log (event_uid, observed_at)
        ~';
    end if;
end;
/

create or replace package slic_zkt_downstream_repair authid definer as
    procedure assert_repairable(
        p_event_uid in varchar2,
        p_old_cnic in varchar2,
        p_new_cnic in varchar2,
        p_attendance_date in date,
        p_old_datasync in number
    );

    procedure repair_identity(
        p_operation_id in varchar2,
        p_event_uid in varchar2,
        p_old_cnic in varchar2,
        p_new_cnic in varchar2,
        p_attendance_date in date,
        p_old_datasync in number
    );

    function projection_digest(
        p_employee_id in number,
        p_attendance_date in date
    ) return varchar2;
end slic_zkt_downstream_repair;
/

create or replace package body slic_zkt_downstream_repair as
    c_attendance_timezone constant varchar2(64) := 'Asia/Karachi';

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

    function resolve_employee(p_cnic in varchar2) return number is
        l_employee_id hr_employee.employee_id%type;
    begin
        if p_cnic is null then
            return null;
        end if;
        if not regexp_like(p_cnic, '^[0-9]{13}$', 'c') then
            raise_application_error(-20540, 'DOWNSTREAM_INVALID_CNIC');
        end if;
        select employee_id
          into l_employee_id
          from hr_employee
         where cnic = to_number(p_cnic, '9999999999999');
        return l_employee_id;
    exception
        when no_data_found then return null;
        when too_many_rows then
            raise_application_error(-20540, 'DOWNSTREAM_DUPLICATE_CNIC');
    end resolve_employee;

    procedure assert_row_repairable(
        p_employee_id in number,
        p_attendance_date in date
    ) is
        l_marked_by hr_employee_attendance.marked_by%type;
        l_check_in hr_employee_attendance.check_in_time%type;
        l_check_out hr_employee_attendance.check_out_time%type;
        l_status_id hr_employee_attendance.status_id%type;
        l_override_type hr_employee_attendance.override_type%type;
        l_override_by hr_employee_attendance.override_by_emp_id%type;
        l_leave_rule hr_employee_attendance.leave_rule_id%type;
        l_leave_application hr_employee_attendance.leave_application_id%type;
        l_payroll_adjusted hr_employee_attendance.payroll_adjusted%type;
        l_payroll_run hr_employee_attendance.payroll_run_id%type;
    begin
        if p_employee_id is null then
            return;
        end if;
        select marked_by, check_in_time, check_out_time, status_id,
               override_type, override_by_emp_id, leave_rule_id,
               leave_application_id, payroll_adjusted, payroll_run_id
          into l_marked_by, l_check_in, l_check_out, l_status_id,
               l_override_type, l_override_by, l_leave_rule,
               l_leave_application, l_payroll_adjusted, l_payroll_run
          from hr_employee_attendance
         where employee_id = p_employee_id
           and attendance_date = trunc(p_attendance_date)
           for update;

        if l_override_type is not null
           or l_override_by is not null
           or l_leave_rule is not null
           or l_leave_application is not null
           or l_status_id = 3
           or nvl(l_payroll_adjusted, 'N') <> 'N'
           or l_payroll_run is not null then
            raise_application_error(-20542, 'DOWNSTREAM_PROTECTED_ATTENDANCE_DAY');
        end if;
        if l_marked_by is not null
           and upper(trim(l_marked_by)) <> 'BIOMETRIC' then
            raise_application_error(-20543, 'DOWNSTREAM_MANUAL_ATTENDANCE_DAY');
        end if;
        if l_marked_by is null
           and (l_check_in is not null or l_check_out is not null) then
            raise_application_error(-20543, 'DOWNSTREAM_UNPROVEN_ATTENDANCE_TIMES');
        end if;
    exception
        when no_data_found then null;
    end assert_row_repairable;

    procedure lock_and_assert_rows(
        p_old_employee_id in number,
        p_new_employee_id in number,
        p_attendance_date in date
    ) is
    begin
        if p_old_employee_id is not null
           and p_old_employee_id <> p_new_employee_id
           and p_old_employee_id < p_new_employee_id then
            assert_row_repairable(p_old_employee_id, p_attendance_date);
            assert_row_repairable(p_new_employee_id, p_attendance_date);
        elsif p_old_employee_id is not null
              and p_old_employee_id <> p_new_employee_id then
            assert_row_repairable(p_new_employee_id, p_attendance_date);
            assert_row_repairable(p_old_employee_id, p_attendance_date);
        else
            assert_row_repairable(p_new_employee_id, p_attendance_date);
        end if;
    end lock_and_assert_rows;

    procedure assert_repairable(
        p_event_uid in varchar2,
        p_old_cnic in varchar2,
        p_new_cnic in varchar2,
        p_attendance_date in date,
        p_old_datasync in number
    ) is
        l_new_employee_id hr_employee.employee_id%type;
        l_old_employee_id hr_employee.employee_id%type;
        l_count number;
    begin
        if p_event_uid is null or p_new_cnic is null or p_attendance_date is null then
            raise_application_error(-20540, 'DOWNSTREAM_REQUIRED_VALUE_MISSING');
        end if;
        l_new_employee_id := resolve_employee(p_new_cnic);
        if l_new_employee_id is null then
            raise_application_error(-20541, 'DOWNSTREAM_TARGET_EMPLOYEE_NOT_FOUND');
        end if;

        if p_old_cnic is not null then
            select count(*)
              into l_count
              from hr_raw_attn_capture_events
             where event_uid = p_event_uid
               and cnic = p_old_cnic
               and nvl(datasync, 0) = nvl(p_old_datasync, 0);
            if l_count <> 1 then
                raise_application_error(-20540, 'DOWNSTREAM_RAW_PRECONDITION_CHANGED');
            end if;
        end if;

        if p_old_cnic is null or p_old_cnic = p_new_cnic then
            l_old_employee_id := case
                when p_old_cnic = p_new_cnic then l_new_employee_id
                else null
            end;
        else
            l_old_employee_id := resolve_employee(p_old_cnic);
            if l_old_employee_id is null and nvl(p_old_datasync, 0) <> 0 then
                raise_application_error(-20541, 'DOWNSTREAM_OLD_PROJECTION_UNRESOLVED');
            end if;
        end if;
        lock_and_assert_rows(
            l_old_employee_id, l_new_employee_id, trunc(p_attendance_date)
        );
    end assert_repairable;

    procedure calculate_status(
        p_employee_id in number,
        p_attendance_date in date,
        p_check_in in date,
        p_check_out in date,
        o_status_id out number,
        o_effective_status out varchar2,
        o_is_holiday out char,
        o_holiday_id out number
    ) is
        l_zone_code number;
        l_iso_day number;
        l_day_flag char(1) := 'Y';
        l_from_minutes number := 540;
        l_to_minutes number := 1020;
        l_duty_minutes number;
        l_shift_minutes number;
    begin
        o_status_id := 2;
        o_effective_status := 'ABSENT';
        o_is_holiday := 'N';
        o_holiday_id := null;

        begin
            select nvl(location.zone_code, -1)
              into l_zone_code
              from hr_employee employee
              left join hr_l_location location
                on location.location_id = employee.location_id
             where employee.employee_id = p_employee_id;
        exception
            when no_data_found then l_zone_code := -1;
        end;

        begin
            select holiday_id
              into o_holiday_id
              from hr_l_holiday_calendar
             where holiday_date = trunc(p_attendance_date)
               and is_active = 'Y'
               and (
                   holiday_type = 'NATIONAL'
                   or (holiday_type = 'ZONE' and zone_code = l_zone_code)
               )
             order by decode(holiday_type, 'ZONE', 1, 'NATIONAL', 2)
             fetch first 1 row only;
            o_is_holiday := 'Y';
            o_status_id := 4;
            o_effective_status := 'HOLIDAY';
            return;
        exception
            when no_data_found then o_is_holiday := 'N';
        end;

        l_iso_day := trunc(p_attendance_date) - trunc(p_attendance_date, 'IW') + 1;
        begin
            select case l_iso_day
                       when 1 then slot.is_monday
                       when 2 then slot.is_tuesday
                       when 3 then slot.is_wednesday
                       when 4 then slot.is_thursday
                       when 5 then slot.is_friday
                       when 6 then slot.is_saturday
                       when 7 then slot.is_sunday
                   end,
                   to_number(substr(slot.from_time, 1, 2)) * 60
                     + to_number(substr(slot.from_time, 4, 2)),
                   to_number(substr(slot.to_time, 1, 2)) * 60
                     + to_number(substr(slot.to_time, 4, 2))
              into l_day_flag, l_from_minutes, l_to_minutes
              from hr_employee_roster roster
              join hr_l_shift_slot slot on slot.slot_id = roster.slot_id
             where roster.employee_id = p_employee_id
               and roster.is_active = 'Y'
               and roster.effective_from <= trunc(p_attendance_date)
               and (
                   roster.effective_to is null
                   or roster.effective_to >= trunc(p_attendance_date)
               )
             fetch first 1 row only;
        exception
            when no_data_found then l_day_flag := 'Y';
        end;

        if nvl(l_day_flag, 'N') = 'N' then
            o_status_id := 5;
            o_effective_status := 'HOLIDAY';
            return;
        end if;
        if p_check_in is null and p_check_out is null then
            o_status_id := 2;
            o_effective_status := 'ABSENT';
            return;
        end if;
        if p_check_in is not null and p_check_out is null then
            o_status_id := 9;
            o_effective_status := 'ABSENT';
            return;
        end if;
        if p_check_in is null and p_check_out is not null then
            raise_application_error(-20544, 'DOWNSTREAM_CHECKOUT_WITHOUT_CHECKIN');
        end if;

        l_duty_minutes := round((p_check_out - p_check_in) * 24 * 60);
        l_shift_minutes := l_to_minutes - l_from_minutes;
        if l_duty_minutes >= round(l_shift_minutes * 0.80) then
            o_status_id := 1;
            o_effective_status := 'PRESENT';
        elsif l_duty_minutes < round(l_shift_minutes * 0.50) then
            o_status_id := 6;
            o_effective_status := 'ABSENT';
        else
            o_status_id := 7;
            o_effective_status := 'PRESENT';
        end if;
    end calculate_status;

    procedure raw_projection(
        p_cnic in varchar2,
        p_attendance_date in date,
        o_event_count out number,
        o_check_in out date,
        o_check_out out date
    ) is
    begin
        select count(*),
               min(case when check_in = 'T' then
                   cast(event_timestamp at time zone 'Asia/Karachi' as date)
               end),
               max(case when check_out = 'T' then
                   cast(event_timestamp at time zone 'Asia/Karachi' as date)
               end)
          into o_event_count, o_check_in, o_check_out
          from hr_raw_attn_capture_events
         where cnic = p_cnic
           and attendance_date = trunc(p_attendance_date)
           and raw_punch = 'F'
           and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                   event_timestamp
               ) = 1;
    end raw_projection;

    procedure project_employee_day(
        p_employee_id in number,
        p_cnic in varchar2,
        p_attendance_date in date,
        o_action out varchar2
    ) is
        l_event_count number;
        l_row_count number;
        l_check_in date;
        l_check_out date;
        l_status_id number;
        l_effective_status varchar2(30);
        l_is_holiday char(1);
        l_holiday_id number;
        l_marked_by hr_employee_attendance.marked_by%type;
    begin
        assert_row_repairable(p_employee_id, p_attendance_date);
        raw_projection(
            p_cnic, p_attendance_date,
            l_event_count, l_check_in, l_check_out
        );
        select count(*)
          into l_row_count
          from hr_employee_attendance
         where employee_id = p_employee_id
           and attendance_date = trunc(p_attendance_date);

        if l_event_count = 0 then
            if l_row_count = 0 then
                o_action := 'NONE';
                return;
            end if;
            select marked_by
              into l_marked_by
              from hr_employee_attendance
             where employee_id = p_employee_id
               and attendance_date = trunc(p_attendance_date)
             for update;
            if upper(trim(nvl(l_marked_by, chr(0)))) = 'BIOMETRIC' then
                delete from hr_employee_attendance
                 where employee_id = p_employee_id
                   and attendance_date = trunc(p_attendance_date);
                o_action := 'REMOVED';
            else
                o_action := 'PRESERVED';
            end if;
            return;
        end if;

        if l_check_in is null then
            raise_application_error(-20544, 'DOWNSTREAM_RAW_FLAGS_INCOMPLETE');
        end if;
        calculate_status(
            p_employee_id, p_attendance_date, l_check_in, l_check_out,
            l_status_id, l_effective_status, l_is_holiday, l_holiday_id
        );

        if l_row_count = 0 then
            insert into hr_employee_attendance (
                employee_id, attendance_date, check_in_time, check_out_time,
                status_id, effective_status, is_holiday_flag, holiday_id,
                created_on, created_by, marked_by
            ) values (
                p_employee_id, trunc(p_attendance_date), l_check_in, l_check_out,
                l_status_id, l_effective_status, l_is_holiday, l_holiday_id,
                sysdate, 'IDENTITY_REPAIR', 'BIOMETRIC'
            );
            o_action := 'INSERTED';
        else
            update hr_employee_attendance set check_in_time = l_check_in,
                   check_out_time = l_check_out,
                   status_id = l_status_id,
                   effective_status = l_effective_status,
                   is_holiday_flag = l_is_holiday,
                   holiday_id = l_holiday_id,
                   marked_by = nvl(marked_by, 'BIOMETRIC'),
                   updated_on = sysdate,
                   updated_by = 'IDENTITY_REPAIR'
             where employee_id = p_employee_id
               and attendance_date = trunc(p_attendance_date);
            o_action := 'UPDATED';
        end if;
    end project_employee_day;

    procedure verify_employee_day(
        p_employee_id in number,
        p_cnic in varchar2,
        p_attendance_date in date
    ) is
        l_event_count number;
        l_check_in date;
        l_check_out date;
        l_status_id number;
        l_effective_status varchar2(30);
        l_is_holiday char(1);
        l_holiday_id number;
        l_count number;
    begin
        raw_projection(
            p_cnic, p_attendance_date,
            l_event_count, l_check_in, l_check_out
        );
        if l_event_count = 0 then
            select count(*)
              into l_count
              from hr_employee_attendance
             where employee_id = p_employee_id
               and attendance_date = trunc(p_attendance_date)
               and upper(trim(nvl(marked_by, chr(0)))) = 'BIOMETRIC';
            if l_count <> 0 then
                raise_application_error(-20544, 'DOWNSTREAM_STALE_BIOMETRIC_ROW');
            end if;
            return;
        end if;

        calculate_status(
            p_employee_id, p_attendance_date, l_check_in, l_check_out,
            l_status_id, l_effective_status, l_is_holiday, l_holiday_id
        );
        select count(*)
          into l_count
          from hr_employee_attendance
         where employee_id = p_employee_id
           and attendance_date = trunc(p_attendance_date)
           and decode(check_in_time, l_check_in, 1, 0) = 1
           and decode(check_out_time, l_check_out, 1, 0) = 1
           and status_id = l_status_id
           and nvl(effective_status, chr(0)) = nvl(l_effective_status, chr(0))
           and is_holiday_flag = l_is_holiday
           and decode(holiday_id, l_holiday_id, 1, 0) = 1;
        if l_count <> 1 then
            raise_application_error(-20544, 'DOWNSTREAM_PROJECTION_VERIFY_FAILED');
        end if;
    end verify_employee_day;

    function projection_digest(
        p_employee_id in number,
        p_attendance_date in date
    ) return varchar2 is
        l_material varchar2(4000);
        l_attendance_id hr_employee_attendance.attendance_id%type;
        l_employee_id hr_employee_attendance.employee_id%type;
        l_day hr_employee_attendance.attendance_date%type;
        l_check_in hr_employee_attendance.check_in_time%type;
        l_check_out hr_employee_attendance.check_out_time%type;
        l_status_id hr_employee_attendance.status_id%type;
        l_effective_status hr_employee_attendance.effective_status%type;
        l_is_holiday hr_employee_attendance.is_holiday_flag%type;
        l_holiday_id hr_employee_attendance.holiday_id%type;
        l_override_type hr_employee_attendance.override_type%type;
        l_leave_rule_id hr_employee_attendance.leave_rule_id%type;
        l_leave_application_id hr_employee_attendance.leave_application_id%type;
        l_payroll_adjusted hr_employee_attendance.payroll_adjusted%type;
        l_payroll_run_id hr_employee_attendance.payroll_run_id%type;
        l_marked_by hr_employee_attendance.marked_by%type;
    begin
        if p_employee_id is null then
            return sha256('NO_EMPLOYEE');
        end if;
        select attendance_id, employee_id, attendance_date,
               check_in_time, check_out_time, status_id, effective_status,
               is_holiday_flag, holiday_id, override_type, leave_rule_id,
               leave_application_id, payroll_adjusted, payroll_run_id, marked_by
          into l_attendance_id, l_employee_id, l_day,
               l_check_in, l_check_out, l_status_id, l_effective_status,
               l_is_holiday, l_holiday_id, l_override_type, l_leave_rule_id,
               l_leave_application_id, l_payroll_adjusted, l_payroll_run_id,
               l_marked_by
          from hr_employee_attendance
         where employee_id = p_employee_id
           and attendance_date = trunc(p_attendance_date);
        l_material := component(to_char(l_attendance_id)) || chr(31)
            || component(to_char(l_employee_id)) || chr(31)
            || component(to_char(l_day, 'YYYY-MM-DD')) || chr(31)
            || component(to_char(l_check_in, 'YYYY-MM-DD"T"HH24:MI:SS')) || chr(31)
            || component(to_char(l_check_out, 'YYYY-MM-DD"T"HH24:MI:SS')) || chr(31)
            || component(to_char(l_status_id)) || chr(31)
            || component(l_effective_status) || chr(31)
            || component(l_is_holiday) || chr(31)
            || component(to_char(l_holiday_id)) || chr(31)
            || component(l_override_type) || chr(31)
            || component(to_char(l_leave_rule_id)) || chr(31)
            || component(to_char(l_leave_application_id)) || chr(31)
            || component(l_payroll_adjusted) || chr(31)
            || component(to_char(l_payroll_run_id)) || chr(31)
            || component(l_marked_by);
        return sha256(l_material);
    exception
        when no_data_found then
            return sha256(
                'ABSENT' || chr(31) || to_char(p_employee_id)
                || chr(31) || to_char(trunc(p_attendance_date), 'YYYY-MM-DD')
            );
    end projection_digest;

    procedure repair_identity(
        p_operation_id in varchar2,
        p_event_uid in varchar2,
        p_old_cnic in varchar2,
        p_new_cnic in varchar2,
        p_attendance_date in date,
        p_old_datasync in number
    ) is
        l_exists number;
        l_new_employee_id hr_employee.employee_id%type;
        l_old_employee_id hr_employee.employee_id%type;
        l_old_resolution varchar2(30);
        l_old_action varchar2(24) := 'NONE';
        l_new_action varchar2(24) := 'NONE';
        l_old_digest varchar2(64);
        l_new_digest varchar2(64);
    begin
        if not regexp_like(
            p_operation_id,
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            'i'
        ) then
            raise_application_error(-20540, 'DOWNSTREAM_INVALID_OPERATION_ID');
        end if;
        select count(*) into l_exists
          from slic_zkt_ds_repair_log
         where operation_id = p_operation_id;
        if l_exists = 1 then
            return;
        end if;

        l_new_employee_id := resolve_employee(p_new_cnic);
        if l_new_employee_id is null then
            raise_application_error(-20541, 'DOWNSTREAM_TARGET_EMPLOYEE_NOT_FOUND');
        end if;

        if p_old_cnic is null then
            l_old_resolution := 'MISSING_RAW';
            l_old_employee_id := null;
        elsif p_old_cnic = p_new_cnic then
            l_old_resolution := 'SAME_EMPLOYEE';
            l_old_employee_id := l_new_employee_id;
        else
            l_old_employee_id := resolve_employee(p_old_cnic);
            if l_old_employee_id is null then
                if nvl(p_old_datasync, 0) <> 0 then
                    raise_application_error(-20541, 'DOWNSTREAM_OLD_PROJECTION_UNRESOLVED');
                end if;
                l_old_resolution := 'UNMAPPED_UNSYNCED';
            else
                l_old_resolution := 'MAPPED_EMPLOYEE';
            end if;
        end if;

        lock_and_assert_rows(
            l_old_employee_id, l_new_employee_id, trunc(p_attendance_date)
        );
        if l_old_employee_id is not null
           and l_old_employee_id <> l_new_employee_id then
            project_employee_day(
                l_old_employee_id, p_old_cnic, p_attendance_date, l_old_action
            );
            verify_employee_day(
                l_old_employee_id, p_old_cnic, p_attendance_date
            );
        elsif l_old_employee_id = l_new_employee_id then
            l_old_action := 'SAME_EMPLOYEE';
        end if;

        project_employee_day(
            l_new_employee_id, p_new_cnic, p_attendance_date, l_new_action
        );
        verify_employee_day(
            l_new_employee_id, p_new_cnic, p_attendance_date
        );

        l_new_digest := projection_digest(l_new_employee_id, p_attendance_date);
        if l_old_employee_id is null then
            l_old_digest := sha256(l_old_resolution || chr(31) || p_operation_id);
        elsif l_old_employee_id = l_new_employee_id then
            l_old_digest := l_new_digest;
        else
            l_old_digest := projection_digest(l_old_employee_id, p_attendance_date);
        end if;

        insert into slic_zkt_ds_repair_log (
            operation_id, event_uid, attendance_date,
            old_employee_id, new_employee_id, old_resolution,
            old_projection_action, new_projection_action,
            old_projection_digest, new_projection_digest,
            downstream_verified, stale_old_identity_absent, observed_at
        ) values (
            p_operation_id, p_event_uid, trunc(p_attendance_date),
            l_old_employee_id, l_new_employee_id, l_old_resolution,
            l_old_action, l_new_action,
            l_old_digest, l_new_digest,
            'T', 'T', systimestamp
        );
    end repair_identity;
end slic_zkt_downstream_repair;
/

create or replace view slic_zkt_repair_downstream_status as
select downstream.operation_id,
       receipt.desired_identity_digest identity_digest,
       case
           when downstream.downstream_verified = 'T'
            and slic_zkt_downstream_repair.projection_digest(
                    downstream.new_employee_id,
                    downstream.attendance_date
                ) = downstream.new_projection_digest
           then 'T' else 'F'
       end downstream_verified,
       case
           when downstream.stale_old_identity_absent = 'T'
            and (
                downstream.old_employee_id is null
                or downstream.old_employee_id = downstream.new_employee_id
                or slic_zkt_downstream_repair.projection_digest(
                       downstream.old_employee_id,
                       downstream.attendance_date
                   ) = downstream.old_projection_digest
            )
           then 'T' else 'F'
       end stale_old_identity_absent,
       downstream.observed_at
  from slic_zkt_ds_repair_log downstream
  join slic_zkt_id_repair_receipts receipt
    on receipt.operation_id = downstream.operation_id;
/

declare
    l_valid number;
    l_columns number;
begin
    select count(*) into l_valid
      from user_objects
     where object_name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
       and object_type in ('PACKAGE', 'PACKAGE BODY')
       and status = 'VALID';
    if l_valid <> 2 then
        raise_application_error(-20545, 'DOWNSTREAM_PACKAGE_INVALID');
    end if;

    select count(*) into l_valid
      from user_objects
     where object_name = 'SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS'
       and object_type = 'VIEW'
       and status = 'VALID';
    if l_valid <> 1 then
        raise_application_error(-20545, 'DOWNSTREAM_STATUS_VIEW_INVALID');
    end if;

    select count(*) into l_columns
      from user_tab_columns
     where table_name = 'SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS'
       and column_name in (
           'OPERATION_ID', 'IDENTITY_DIGEST', 'DOWNSTREAM_VERIFIED',
           'STALE_OLD_IDENTITY_ABSENT', 'OBSERVED_AT'
       );
    if l_columns <> 5 then
        raise_application_error(-20545, 'DOWNSTREAM_STATUS_VIEW_SHAPE_INVALID');
    end if;

    dbms_output.put_line('DOWNSTREAM_ADAPTER_READY=TRUE');
    dbms_output.put_line('NO_ATTENDANCE_DATA_MUTATED_BY_INSTALL=TRUE');
end;
/
