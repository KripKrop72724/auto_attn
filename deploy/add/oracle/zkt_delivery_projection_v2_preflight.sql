-- Read-only syntax/column check: every production table is forced to zero rows.
-- No package is installed/called and no attendance data is returned.
with people as (
    select count(*) amount, min(employee_id) employee_id from hr_employee where 1=0
), normal as (
    select event_uid, event_timestamp,
           row_number() over(order by event_timestamp,event_uid) first_rank,
           row_number() over(order by event_timestamp desc,event_uid desc) last_rank,
           case when trust_status='SUSPECT_DEVICE_TIME'
                or trunc(cast(event_timestamp at time zone 'Asia/Karachi' as date))<>attendance_date
                then 1 else 0 end invalid
      from hr_raw_attn_capture_events where 1=0
), totals as (
    select count(*) amount,
           max(case when first_rank=1 then event_uid end) first_uid,
           max(case when last_rank=1 then event_uid end) last_uid,
           min(cast(event_timestamp at time zone 'Asia/Karachi' as date)) first_time,
           max(cast(event_timestamp at time zone 'Asia/Karachi' as date)) last_time,
           nvl(sum(invalid),0) invalid from normal
), day_row as (
    select count(*) amount,min(check_in_time) first_time,max(check_out_time) last_time,
           nvl(max(case when nvl(marked_by,'?')<>'BIOMETRIC'
                        or leave_application_id is not null or od_request_id is not null
                        then 1 else 0 end),0) protected
      from hr_employee_attendance where 1=0
), projected as (
    select d.zone_id,d.device_id,d.device_serial,d.user_id,d.employee_name,d.cnic,
           d.event_timestamp,d.raw_punch,d.capture_type,d.trust_status,d.clock_diff_seconds,
           d.attendance_date,d.received_at,d.check_in,d.check_out,d.datasync,
           count(*) over() target_count,p.amount people_count,p.employee_id,
           n.amount normal_count,n.first_uid,n.last_uid,n.first_time,n.last_time,n.invalid,
           a.amount daily_count,a.first_time daily_first,a.last_time daily_last,a.protected
      from hr_raw_attn_capture_events d cross join people p cross join totals n cross join day_row a
     where 1=0
)
select 'ZKT_V2_SQL_PARSED_NO_ATTENDANCE_READ' validation_result,count(*) matched_rows,
       to_char(from_tz(to_timestamp('2026-10-01T04:00:00.123456','YYYY-MM-DD"T"HH24:MI:SS.FF6'),'UTC')
          at time zone 'Asia/Karachi','YYYY-MM-DD"T"HH24:MI:SS.FF6') synthetic_local_time
  from projected;
