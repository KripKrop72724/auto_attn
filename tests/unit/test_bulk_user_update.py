import pytest

from zk_zone_agent.bulk_user_update import (
    ExportUserRow,
    build_machine_name,
    export_users_xlsx,
    normalize_cnic,
    parse_bulk_update_xlsx,
    split_machine_name_cnic,
)


def test_split_machine_name_cnic_detects_suffix():
    assert split_machine_name_cnic("Ali Khan-3520212345671") == ("Ali Khan", "3520212345671")
    assert split_machine_name_cnic("Ali Khan") == ("Ali Khan", "")


def test_normalize_cnic_accepts_formatted_value():
    assert normalize_cnic("35202-1234567-1") == "3520212345671"


def test_normalize_cnic_rejects_wrong_digit_count():
    with pytest.raises(ValueError, match="13 digits"):
        normalize_cnic("12345")


def test_build_machine_name_truncates_prefix_to_device_limit():
    assert build_machine_name("Muhammad Abdullah Khan", "3520212345671") == "Muhammad A-3520212345671"
    assert len(build_machine_name("Muhammad Abdullah Khan", "3520212345671").encode("utf-8")) == 24


def test_parse_bulk_update_xlsx_skips_blank_cnic_and_normalizes_valid_rows():
    content = export_users_xlsx(
        [
            ExportUserRow(user_id="1001", name="Ali Khan", cnic="35202-1234567-1"),
            ExportUserRow(user_id="1002", name="Sara", cnic=""),
        ]
    )

    rows = parse_bulk_update_xlsx(content)

    assert rows[0].status == "PENDING"
    assert rows[0].cnic == "3520212345671"
    assert rows[0].expected_name == "Ali Khan-3520212345671"
    assert rows[1].status == "SKIPPED"
    assert rows[1].message == "CNIC is blank; row skipped."


def test_parse_bulk_update_xlsx_flags_duplicate_ids():
    content = export_users_xlsx(
        [
            ExportUserRow(user_id="1001", name="Ali", cnic="3520212345671"),
            ExportUserRow(user_id="1001", name="Ali 2", cnic="3520212345672"),
        ]
    )

    rows = parse_bulk_update_xlsx(content)

    assert rows[0].status == "PENDING"
    assert rows[1].status == "FAILED"
    assert "Duplicate ID 1001" in rows[1].message
