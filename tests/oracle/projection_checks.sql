declare
    l_errors number;
begin
    select count(*) into l_errors from all_errors where owner = 'ZKT_SYNTHETIC'
      and name = 'SLIC_ZKT_DELIVERY_V2';
    if l_errors <> 0 then raise_application_error(-20699, 'PACKAGE_COMPILATION_FAILED'); end if;
end;
/
declare
    l_payload clob := '{"contract_version":"2","verification_scope":"ORACLE_RAW_DAY_TIMES_V2","request_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","projection":{"event_uid":"1111111111111111111111111111111111111111111111111111111111111111","zone_id":"TEST-ZONE","device_id":"TEST-DEVICE","device_serial":"TEST-SERIAL","user_id":"1007","employee_name":"Synthetic Person","cnic":"1234512345671","timestamp":"2026-10-01T04:00:00.000000Z","raw_punch":"F","capturetype":"LIVE","trust_status":"TRUSTED_LIVE","clockdiff":"0.000"}}';
    l_request json_object_t;
    l_projection json_object_t;
    l_result json_object_t;
    l_token varchar2(64);
    l_checked pls_integer := 0;
    l_count number;
    l_started number;
    procedure check_state(p_class in varchar2, p_raw in boolean, p_day in varchar2 default 'NOT_VERIFIED', p_body in clob default null) is
        l_response json_object_t;
        l_results json_array_t;
    begin
        l_response := json_object_t.parse(slic_zkt_delivery_v2.inspect_projection(coalesce(p_body,l_payload)));
        l_results := l_response.get_array('results');
        l_result := treat(l_results.get(0) as json_object_t);
        if l_result.get_string('classification') <> p_class
           or l_result.get_boolean('raw_projection_verified') <> p_raw
           or l_result.get_string('downstream_status') <> p_day
           or l_response.get_string('contract_version') <> '2'
           or l_response.get_string('verification_scope') <> 'ORACLE_RAW_DAY_TIMES_V2'
           or l_response.get_string('request_digest') <> rpad('a',64,'a')
           or not regexp_like(l_result.get_string('current_content_token'),'^[0-9a-f]{64}$','c') then
            raise_application_error(-20699, 'UNEXPECTED_PROJECTION: ' || l_result.to_string);
        end if;
        l_checked := l_checked + 1;
    end;
    procedure check_bad_request(p_body in clob) is
        l_response clob;
    begin
        begin
            l_response := slic_zkt_delivery_v2.inspect_projection(p_body);
        exception when others then l_checked := l_checked + 1; return;
        end;
        raise_application_error(-20699,'INVALID_REQUEST_ACCEPTED');
    end;
begin
    check_state('MISSING',false);
    insert into hr_employee values (7,1234512345671);
    insert into hr_raw_attn_capture_events(event_uid,zone_id,device_id,device_serial,user_id,employee_name,cnic,
        event_timestamp,clock_diff_seconds,capture_type,trust_status,received_at,attendance_date,check_in,check_out,raw_punch,datasync)
    values(rpad('1',64,'1'),'TEST-ZONE','TEST-DEVICE','TEST-SERIAL','1007','Synthetic Person',1234512345671,
        to_timestamp_tz('2026-10-01 04:00:00 +00:00','YYYY-MM-DD HH24:MI:SS TZH:TZM'),0,'LIVE','TRUSTED_LIVE',
        systimestamp,date '2026-10-01','T','F','F',1);
    check_state('DOWNSTREAM_PENDING',true);
    insert into hr_employee_attendance(employee_id,attendance_date,check_in_time,marked_by)
        values(7,date '2026-10-01',to_date('2026-10-01 09:00:00','YYYY-MM-DD HH24:MI:SS'),'BIOMETRIC');
    check_state('MATCH',true,'MATCH');
    l_token := l_result.get_string('current_content_token');
    check_state('MATCH',true,'MATCH');
    if l_token <> l_result.get_string('current_content_token') then raise_application_error(-20699,'UNSTABLE_TOKEN'); end if;
    savepoint preserved;
    update hr_raw_attn_capture_events set zone_id='WRONG'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set device_id='WRONG'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set device_serial='WRONG'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set device_serial=null; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set user_id='WRONG'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set employee_name='WRONG'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set employee_name=null; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set cnic=1234512345672; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set event_timestamp=event_timestamp+interval '1' second; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set clock_diff_seconds=0.001; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set clock_diff_seconds=null; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set capture_type='DUMP_RECONNECT'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set trust_status='SUSPECT_DEVICE_TIME'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set attendance_date=date '2026-10-02'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set raw_punch='T'; check_state('MISMATCH',false); rollback to preserved;
    update hr_raw_attn_capture_events set datasync=0; check_state('DOWNSTREAM_PENDING',true); rollback to preserved;
    update hr_raw_attn_capture_events set check_in='F'; check_state('DOWNSTREAM_PENDING',true); rollback to preserved;
    update hr_employee_attendance set check_in_time=check_in_time+1/86400; check_state('DOWNSTREAM_PENDING',true); rollback to preserved;
    update hr_employee_attendance set check_out_time=check_in_time; check_state('DOWNSTREAM_PENDING',true); rollback to preserved;
    update hr_employee_attendance set marked_by='MANUAL'; check_state('DOWNSTREAM_HOLD',true); rollback to preserved;
    update hr_employee_attendance set leave_application_id=12; check_state('DOWNSTREAM_HOLD',true); rollback to preserved;
    update hr_employee_attendance set od_request_id=13; check_state('DOWNSTREAM_HOLD',true); rollback to preserved;
    insert into hr_employee values(8,1234512345671); check_state('IDENTITY_HOLD',true); rollback to preserved;
    delete from hr_employee; check_state('IDENTITY_HOLD',true); rollback to preserved;

    -- Two separate occurrences in the same second retain their two identities.
    insert into hr_raw_attn_capture_events(event_uid,zone_id,device_id,device_serial,user_id,employee_name,cnic,
        event_timestamp,clock_diff_seconds,capture_type,trust_status,received_at,attendance_date,check_in,check_out,raw_punch,datasync)
    select rpad('2',64,'2'),zone_id,device_id,device_serial,user_id,employee_name,cnic,event_timestamp,
        clock_diff_seconds,capture_type,trust_status,received_at,attendance_date,'F','T',raw_punch,1
        from hr_raw_attn_capture_events;
    check_state('DOWNSTREAM_PENDING',true);
    update hr_employee_attendance set check_out_time=check_in_time;
    check_state('MATCH',true,'MATCH');
    if l_token = l_result.get_string('current_content_token') then raise_application_error(-20699,'DAY_CHANGE_NOT_BOUND'); end if;
    rollback to preserved;

    -- A raw-only observation is preserved without inventing a daily attendance.
    l_request:=json_object_t.parse(l_payload); l_projection:=l_request.get_object('projection');
    l_projection.put('raw_punch','T'); l_request.put('projection',l_projection);
    update hr_raw_attn_capture_events set raw_punch='T',check_in='F',check_out='F';
    check_state('MATCH',true,'RAW_ONLY',l_request.to_clob); rollback to preserved;
    l_projection.put('raw_punch','F'); l_projection.put_null('clockdiff'); l_request.put('projection',l_projection);
    update hr_raw_attn_capture_events set clock_diff_seconds=null;
    check_state('MATCH',true,'MATCH',l_request.to_clob); rollback to preserved;

    -- Formatting does not depend on the session timezone or numeric locale.
    execute immediate q'!alter session set time_zone = '-07:00'!';
    execute immediate q'!alter session set nls_numeric_characters = ',.'!';
    check_state('MATCH',true,'MATCH');
    if l_token <> l_result.get_string('current_content_token') then raise_application_error(-20699,'LOCALE_CHANGED_PROOF'); end if;
    check_bad_request(replace(l_payload,'"contract_version":"2"','"contract_version":"1"'));
    check_bad_request(replace(l_payload,'2026-10-01T04:00:00.000000Z','2026-02-30T04:00:00.000000Z'));
    check_bad_request(replace(l_payload,'"clockdiff":"0.000"','"clockdiff":true'));
    check_bad_request(replace(l_payload,'"zone_id":"TEST-ZONE"','"zone_id":null'));
    -- Retained volume belongs to many days; the proof reads only the affected
    -- employee/day. This measured case is not a p99 field qualification.
    insert into hr_raw_attn_capture_events(event_uid,zone_id,device_id,device_serial,user_id,employee_name,cnic,
        event_timestamp,clock_diff_seconds,capture_type,trust_status,received_at,attendance_date,check_in,check_out,raw_punch,datasync)
    select lpad(to_char(level,'FM999999'),64,'0'),'TEST-ZONE','TEST-DEVICE','TEST-SERIAL','1007','Synthetic Person',1234512345671,
        from_tz(timestamp '2026-10-01 04:00:00','UTC') + numtodsinterval(floor((level-1)/2000),'DAY')
            + numtodsinterval(mod(level-1,2000)+1,'SECOND'),
        0,'LIVE','TRUSTED_LIVE',systimestamp,date '2026-10-01'+floor((level-1)/2000),'F','F','F',1
        from dual connect by level <= 200000;
    update hr_employee_attendance set check_out_time=check_in_time+2000/86400;
    l_started := dbms_utility.get_time;
    check_state('MATCH',true,'MATCH');
    dbms_output.put_line('retained_rows=200001 affected_day_rows=2001 reader_ms=' || ((dbms_utility.get_time-l_started)*10));
    if dbms_utility.get_time-l_started > 6000 then raise_application_error(-20699,'SYNTHETIC_READ_DEADLINE'); end if;
    rollback;
    select count(*) into l_count from hr_raw_attn_capture_events;
    if l_count <> 0 then raise_application_error(-20699,'READER_COMMITTED_SYNTHETIC_ROWS'); end if;
    dbms_output.put_line('ORACLE_PROJECTION_CHECKS_PASSED count=' || l_checked);
end;
/

declare
    l_names owa.vc_arr;
    l_values owa.vc_arr;
    l_output htp.htbuf_arr;
    l_lines integer := 100;
    l_text varchar2(32767);
    procedure check_auth(p_password in varchar2, p_code in varchar2, p_status in varchar2) is
    begin
        htp.init;
        l_names(1):='HTTP_X_API_USERNAME'; l_values(1):='synthetic-add';
        l_names(2):='HTTP_X_API_PASSWORD'; l_values(2):=p_password;
        owa.init_cgi_env(2,l_names,l_values);
        slic_zkt_delivery_v2.post_check('{}');
        l_lines := 100; htp.get_page(l_output,l_lines); l_text := null;
        for i in 1..l_lines loop l_text := l_text || l_output(i); end loop;
        if instr(l_text,p_code)=0 or instr(l_text,'Status: '||p_status)=0 then
            raise_application_error(-20699,'AUTH_WRAPPER_FAILED');
        end if;
    end;
begin
    check_auth('wrong-password','ADD_ONLY_AUTH_REQUIRED','401');
    check_auth('synthetic-test-password','PROJECTION_CHECK_FAILED','400');
    dbms_output.put_line('ORACLE_AUTH_CHECKS_PASSED count=2');
end;
/
