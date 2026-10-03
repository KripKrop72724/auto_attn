set define off
set serveroutput on
whenever sqlerror exit failure rollback

-- Separate, manual installation after qualified package/credential review.
-- Never invoked by the ADD deployment. Only defines the new reader route.
-- No existing attendance handler or production data is changed by this file.
declare
    l_count number;
    l_module user_ords_modules.name%type;
begin
    select count(*) into l_count from user_objects where object_name = 'SLIC_ZKT_DELIVERY_V2'
      and object_type in ('PACKAGE','PACKAGE BODY') and status = 'VALID';
    if l_count <> 2 then raise_application_error(-20679,'DELIVERY_READER_NOT_VALID'); end if;
    select count(*) into l_count from user_source where name = 'SLIC_ZKT_DELIVERY_V2'
      and type = 'PACKAGE BODY' and instr(text,'REPLACE_WITH_ADD_') > 0;
    if l_count <> 0 then raise_application_error(-20679,'ADD_ONLY_CREDENTIAL_NOT_CONFIGURED'); end if;
    select count(*),min(name) into l_count,l_module from user_ords_modules
      where uri_prefix = '/raw_attn_capture_event/';
    if l_count <> 1 then raise_application_error(-20679,'RAW_ATTENDANCE_MODULE_AMBIGUOUS'); end if;
    ords.define_template(p_module_name => l_module, p_pattern => 'raw-captures/delivery-v2/check');
    ords.define_handler(p_module_name => l_module, p_pattern => 'raw-captures/delivery-v2/check',
        p_method => 'POST', p_source_type => ords.source_type_plsql, p_items_per_page => 0,
        p_source => 'begin slic_zkt_delivery_v2.post_check(:body_text); end;');
    commit;
exception when others then rollback; raise;
end;
/
