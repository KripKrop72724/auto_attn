-- Production SLIC_HRM.SLIC_ZKT_RECOMPUTE_DAILY_FLAGS as read from USER_SOURCE on 13 Nov 2026 ~23:30 PKT
-- (last DDL 2026-10-01 19:25). Whitespace normalised by the read; logic unchanged. This is the UNDO copy.
create or replace procedure slic_zkt_recompute_daily_flags(
  p_body in clob
) authid definer
as
begin
  for affected_day in (
    select distinct
      trim(j.cnic) cnic,
      slic_zkt_truth_api.attendance_date_for(
        slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)
      ) attendance_date
    from json_table(
      p_body,
      '$.events[*]'
      columns
        cnic varchar2(13) path '$.cnic' null on error,
        event_timestamp varchar2(80) path '$.timestamp' null on error
    ) j
    where trim(j.cnic) is not null
      and slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) is not null
    order by cnic, attendance_date
  ) loop
    for locked_event in (
      select d.event_uid
      from hr_raw_attn_capture_events d
      where d.raw_punch = 'F'
        and slic_zkt_truth_api.attendance_timestamp_is_plausible(
          d.event_timestamp
        ) = 1
        and d.cnic = affected_day.cnic
        and d.attendance_date = affected_day.attendance_date
      order by d.event_timestamp, d.event_uid
      for update
    ) loop
      null;
    end loop;
  end loop;

  update hr_raw_attn_capture_events d
  set d.check_in = 'F',
      d.check_out = 'F',
      d.datasync = 0
  where d.raw_punch = 'F'
    and slic_zkt_truth_api.attendance_timestamp_is_plausible(
      d.event_timestamp
    ) = 1
    and (d.check_in <> 'F' or d.check_out <> 'F')
    and exists (
      select 1
      from json_table(
        p_body,
        '$.events[*]'
        columns
          cnic varchar2(13) path '$.cnic' null on error,
          event_timestamp varchar2(80) path '$.timestamp' null on error
      ) j
      where trim(j.cnic) = d.cnic
        and slic_zkt_truth_api.attendance_date_for(
          slic_zkt_truth_api.parse_event_timestamp(
            j.event_timestamp
          )
        ) = d.attendance_date
    );

  merge into hr_raw_attn_capture_events d
  using (
    select ranked.event_uid,
      case when ranked.rn_in = 1 then 'T' else 'F' end check_in,
      case
        when ranked.normal_count > 1 and ranked.rn_out = 1
        then 'T'
        else 'F'
      end check_out
    from (
      select stored.event_uid,
        row_number() over (
          partition by stored.cnic, stored.attendance_date
          order by stored.event_timestamp, stored.event_uid
        ) rn_in,
        row_number() over (
          partition by stored.cnic, stored.attendance_date
          order by stored.event_timestamp desc, stored.event_uid desc
        ) rn_out,
        count(*) over (
          partition by stored.cnic, stored.attendance_date
        ) normal_count
      from hr_raw_attn_capture_events stored
      where stored.raw_punch = 'F'
        and slic_zkt_truth_api.attendance_timestamp_is_plausible(
          stored.event_timestamp
        ) = 1
        and exists (
          select 1
          from json_table(
            p_body,
            '$.events[*]'
            columns
              cnic varchar2(13) path '$.cnic' null on error,
              event_timestamp varchar2(80) path '$.timestamp' null on error
          ) j
          where trim(j.cnic) = stored.cnic
            and slic_zkt_truth_api.attendance_date_for(
              slic_zkt_truth_api.parse_event_timestamp(
                j.event_timestamp
              )
            ) = stored.attendance_date
        )
    ) ranked
  ) desired
  on (d.event_uid = desired.event_uid)
  when matched then update set
    d.check_in = desired.check_in,
    d.check_out = desired.check_out,
    d.datasync = 0
  where d.check_in <> desired.check_in
     or d.check_out <> desired.check_out;
end slic_zkt_recompute_daily_flags;
/
