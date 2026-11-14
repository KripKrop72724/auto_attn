-- Runs just before the migration in RECOMP_BUSY. Waits until the holding
-- session has been inside RECOMP_BUSY.SLIC_ZKT_IDENTITY_REPAIR_API for more
-- than 62 s, so the migration's 60 s long-call guard has to see it.
declare
    l_seen number;
    l_waited pls_integer := 0;
begin
    loop
        select count(*)
          into l_seen
          from v$session s
         where s.status = 'ACTIVE'
           and s.last_call_et > 62
           and s.plsql_entry_object_id in (
                   select o.object_id
                     from dba_objects o
                    where o.owner = 'RECOMP_BUSY'
                      and o.object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
               );
        exit when l_seen > 0;
        if l_waited >= 150 then
            for s in (
                select sid, status, last_call_et, plsql_entry_object_id,
                       plsql_object_id, module, event
                  from v$session
                 where type = 'USER'
                 order by last_call_et desc
            ) loop
                dbms_output.put_line(
                    'recompute_busy_session sid=' || s.sid || ' status=' || s.status
                    || ' last_call_et=' || s.last_call_et
                    || ' entry_object_id=' || s.plsql_entry_object_id
                    || ' object_id=' || s.plsql_object_id
                    || ' module=' || s.module || ' event=' || s.event
                );
            end loop;
            raise_application_error(
                -20143,
                'The holding session never showed up in V$SESSION as a call of more than'
                || ' 62 s in RECOMP_BUSY.SLIC_ZKT_IDENTITY_REPAIR_API.'
            );
        end if;
        dbms_session.sleep(1);
        l_waited := l_waited + 1;
    end loop;
    dbms_output.put_line('recompute_busy_holder=seen after ' || l_waited || ' s');
end;
/
