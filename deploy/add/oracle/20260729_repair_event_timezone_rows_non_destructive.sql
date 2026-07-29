set define off
set serveroutput on

/*
  Non-destructive production repair for HR_RAW_ATTN_CAPTURE_EVENTS.

  The event instants are already correct. This transaction:
  - normalizes UTC-represented TSTZ values to Asia/Karachi without arithmetic;
  - corrects ATTENDANCE_DATE only for the eight preflight-proven event UIDs;
  - recomputes first/last daily flags for both the old and corrected dates;
  - snapshots every normalized event's UTC instant and proves it is unchanged;
  - proves row count and event UID uniqueness are unchanged; and
  - rolls the entire DML transaction back on any failed invariant.

  No row is inserted or deleted.
*/

declare
    type t_instant_record is record (
        event_uid hr_raw_attn_capture_events.event_uid%type,
        utc_value timestamp
    );
    type t_instant_table is table of t_instant_record index by pls_integer;
    type t_day_record is record (
        cnic hr_raw_attn_capture_events.cnic%type,
        attendance_date date
    );
    type t_day_map is table of t_day_record index by varchar2(64);

    l_instants                 t_instant_table;
    l_days                     t_day_map;
    l_instant_count            pls_integer := 0;
    l_day_key                  varchar2(64);
    l_total_before             number;
    l_total_after              number;
    l_duplicate_before         number;
    l_duplicate_after          number;
    l_null_uid_before          number;
    l_utc_before               number;
    l_utc_after                number;
    l_mismatch_before          number;
    l_mismatch_after           number;
    l_unknown_mismatches       number;
    l_updated_timezone_rows    number;
    l_updated_date_rows        number;
    l_after_utc                timestamp;
    l_flag_errors              number;

    procedure remember_day(
        p_cnic in hr_raw_attn_capture_events.cnic%type,
        p_date in date
    ) is
        l_key varchar2(64);
    begin
        if p_cnic is null or p_date is null then
            return;
        end if;
        l_key := trim(p_cnic) || '|' || to_char(p_date, 'YYYYMMDD');
        l_days(l_key).cnic := trim(p_cnic);
        l_days(l_key).attendance_date := trunc(p_date);
    end remember_day;

    procedure recompute_day(
        p_cnic in hr_raw_attn_capture_events.cnic%type,
        p_date in date
    ) is
    begin
        for locked_event in (
            select d.event_uid
              from hr_raw_attn_capture_events d
             where d.raw_punch = 'F'
               and d.cnic = p_cnic
               and d.attendance_date = p_date
             order by d.event_timestamp, d.event_uid
             for update
        ) loop
            null;
        end loop;

        update hr_raw_attn_capture_events d
           set d.check_in = 'F',
               d.check_out = 'F',
               d.datasync = 0
         where d.raw_punch = 'F'
           and d.cnic = p_cnic
           and d.attendance_date = p_date;

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
                             order by stored.event_timestamp, stored.event_uid
                         ) rn_in,
                         row_number() over (
                             order by stored.event_timestamp desc, stored.event_uid desc
                         ) rn_out,
                         count(*) over () normal_count
                    from hr_raw_attn_capture_events stored
                   where stored.raw_punch = 'F'
                     and stored.cnic = p_cnic
                     and stored.attendance_date = p_date
              ) ranked
        ) desired
           on (d.event_uid = desired.event_uid)
         when matched then update set
              d.check_in = desired.check_in,
              d.check_out = desired.check_out,
              d.datasync = 0;

        select count(*)
          into l_flag_errors
          from (
              select stored.check_in,
                     stored.check_out,
                     case when row_number() over (
                         order by stored.event_timestamp, stored.event_uid
                     ) = 1 then 'T' else 'F' end expected_check_in,
                     case
                         when count(*) over () > 1
                          and row_number() over (
                              order by stored.event_timestamp desc,
                                       stored.event_uid desc
                          ) = 1
                         then 'T'
                         else 'F'
                     end expected_check_out
                from hr_raw_attn_capture_events stored
               where stored.raw_punch = 'F'
                 and stored.cnic = p_cnic
                 and stored.attendance_date = p_date
          )
         where nvl(check_in, '?') <> expected_check_in
            or nvl(check_out, '?') <> expected_check_out;

        if l_flag_errors <> 0 then
            raise_application_error(
                -20515,
                'Daily flag verification failed for an affected CNIC/date.'
            );
        end if;
    end recompute_day;
begin
    lock table hr_raw_attn_capture_events
        in share row exclusive mode wait 30;

    select count(*),
           count(*) - count(distinct event_uid),
           sum(case when event_uid is null then 1 else 0 end),
           sum(case
                   when to_char(event_timestamp, 'TZH:TZM') = '+00:00'
                   then 1 else 0
               end),
           sum(case
                   when attendance_date is null
                     or attendance_date <> trunc(
                            cast(
                                event_timestamp at time zone 'Asia/Karachi'
                                as date
                            )
                        )
                   then 1 else 0
               end)
      into l_total_before,
           l_duplicate_before,
           l_null_uid_before,
           l_utc_before,
           l_mismatch_before
      from hr_raw_attn_capture_events;

    if l_duplicate_before <> 0 or l_null_uid_before <> 0 then
        raise_application_error(
            -20510,
            'Preflight failed: duplicate or null event UIDs exist.'
        );
    end if;

    select count(*)
      into l_unknown_mismatches
      from hr_raw_attn_capture_events
     where (
               attendance_date is null
               or attendance_date <> trunc(
                      cast(
                          event_timestamp at time zone 'Asia/Karachi'
                          as date
                      )
                  )
           )
       and (
           event_uid is null
           or event_uid not in (
               'dff1455669191f906988cff77516939fb5b4df1c6c53560fb7df95e8851a4cc5',
               '6d94f6af8d27541c543aa4f32e1168a96701b07a4a7accb47672c9fa0f7bb700',
               '92c9531e11b8c180c5d7984551a6fe85cb444f7b32423340bb59ab1a4e1d7607',
               '43af66dc63e47f546a4a6df271ee848ed2992d2422760b40f3a16e30faf9b7db',
               'bf1c11b735220c3aa56626ee58880207a3373eecd601c95a295da89e77c2a6b3',
               '1e2761a1c934774797634205b8d264361742bb1c043ae91c1d515295ccdfaabf',
               '12c5b5eb97688657ab1098f79deb85f752b131ca0c1f214b10c5576af5cd8081',
               '1beb977ed9eb34e56bc558b935cbddaf539e58b8863b059e94b8dd78ff172c6a'
           )
       );

    if l_unknown_mismatches <> 0 or l_mismatch_before > 8 then
        raise_application_error(
            -20511,
            'Preflight failed: attendance-date mismatch set differs from the reviewed allow-list.'
        );
    end if;

    for saved_event in (
        select event_uid,
               sys_extract_utc(event_timestamp) utc_value
          from hr_raw_attn_capture_events
         where to_char(event_timestamp, 'TZH:TZM') = '+00:00'
         order by event_uid
    ) loop
        l_instant_count := l_instant_count + 1;
        l_instants(l_instant_count).event_uid := saved_event.event_uid;
        l_instants(l_instant_count).utc_value := saved_event.utc_value;
    end loop;

    if l_instant_count <> l_utc_before then
        raise_application_error(
            -20512,
            'Preflight failed: UTC representation snapshot count changed.'
        );
    end if;

    for affected_event in (
        select cnic,
               attendance_date old_date,
               trunc(
                   cast(
                       event_timestamp at time zone 'Asia/Karachi'
                       as date
                   )
               ) new_date
          from hr_raw_attn_capture_events
         where attendance_date is null
            or attendance_date <> trunc(
                   cast(
                       event_timestamp at time zone 'Asia/Karachi'
                       as date
                   )
               )
    ) loop
        remember_day(affected_event.cnic, affected_event.old_date);
        remember_day(affected_event.cnic, affected_event.new_date);
    end loop;

    update hr_raw_attn_capture_events
       set event_timestamp =
               event_timestamp at time zone 'Asia/Karachi'
     where to_char(event_timestamp, 'TZH:TZM') = '+00:00';
    l_updated_timezone_rows := sql%rowcount;

    if l_updated_timezone_rows <> l_utc_before then
        raise_application_error(
            -20513,
            'Timezone normalization count differed from the locked preflight.'
        );
    end if;

    update hr_raw_attn_capture_events
       set attendance_date = trunc(
               cast(
                   event_timestamp at time zone 'Asia/Karachi'
                   as date
               )
           )
     where attendance_date is null
        or attendance_date <> trunc(
               cast(
                   event_timestamp at time zone 'Asia/Karachi'
                   as date
               )
           );
    l_updated_date_rows := sql%rowcount;

    if l_updated_date_rows <> l_mismatch_before then
        raise_application_error(
            -20514,
            'Attendance-date repair count differed from the locked preflight.'
        );
    end if;

    l_day_key := l_days.first;
    while l_day_key is not null loop
        recompute_day(
            l_days(l_day_key).cnic,
            l_days(l_day_key).attendance_date
        );
        l_day_key := l_days.next(l_day_key);
    end loop;

    if l_instant_count > 0 then
        for i in 1 .. l_instant_count loop
            select sys_extract_utc(event_timestamp)
              into l_after_utc
              from hr_raw_attn_capture_events
             where event_uid = l_instants(i).event_uid;

            if l_after_utc <> l_instants(i).utc_value then
                raise_application_error(
                    -20516,
                    'Event instant changed during timezone normalization.'
                );
            end if;
        end loop;
    end if;

    select count(*),
           count(*) - count(distinct event_uid),
           sum(case
                   when to_char(event_timestamp, 'TZH:TZM') = '+00:00'
                   then 1 else 0
               end),
           sum(case
                   when attendance_date is null
                     or attendance_date <> trunc(
                            cast(
                                event_timestamp at time zone 'Asia/Karachi'
                                as date
                            )
                        )
                   then 1 else 0
               end)
      into l_total_after,
           l_duplicate_after,
           l_utc_after,
           l_mismatch_after
      from hr_raw_attn_capture_events;

    if l_total_after <> l_total_before
       or l_duplicate_after <> l_duplicate_before
       or l_utc_after <> 0
       or l_mismatch_after <> 0 then
        raise_application_error(
            -20517,
            'Post-repair row-count, UID, timezone, or attendance-date invariant failed.'
        );
    end if;

    commit;
    dbms_output.put_line(
        'Committed non-destructive timestamp repair: '
        || l_updated_timezone_rows
        || ' representations normalized, '
        || l_updated_date_rows
        || ' attendance dates corrected, '
        || l_total_after
        || ' rows preserved.'
    );
exception
    when others then
        rollback;
        raise;
end;
/
