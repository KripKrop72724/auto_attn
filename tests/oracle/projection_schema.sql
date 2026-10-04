alter session set container = FREEPDB1;
begin
    execute immediate 'drop user zkt_synthetic cascade';
exception when others then if sqlcode <> -1918 then raise; end if;
end;
/
create user zkt_synthetic no authentication;
grant unlimited tablespace to zkt_synthetic;
grant create table, create procedure to zkt_synthetic;
alter session set current_schema = zkt_synthetic;
create table hr_employee (employee_id number primary key, cnic number);
create table hr_raw_attn_capture_events (
    raw_capture_id number generated always as identity primary key,
    event_uid varchar2(600) not null,
    zone_id varchar2(200) not null, device_id varchar2(400) not null,
    device_serial varchar2(400), user_id varchar2(200) not null,
    employee_name varchar2(800), cnic number not null,
    event_timestamp timestamp(6) with time zone not null,
    clock_diff_seconds number(10,3), capture_type varchar2(120) not null,
    trust_status varchar2(240) not null, received_at timestamp(6) with time zone not null,
    attendance_date date not null, check_in varchar2(4) not null,
    check_out varchar2(4) not null, raw_punch varchar2(4) not null, datasync number,
    constraint raw_uid_unique unique (event_uid)
);
create index raw_day_idx on hr_raw_attn_capture_events(cnic, attendance_date);
create table hr_employee_attendance (
    attendance_id number generated always as identity primary key,
    employee_id number not null, attendance_date date not null,
    check_in_time date, check_out_time date, marked_by varchar2(30),
    leave_application_id number, od_request_id number,
    constraint daily_unique unique (employee_id, attendance_date)
);
