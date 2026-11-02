"""Names and checks the screens show (no Qt needed)."""

from __future__ import annotations

import pytest

from sl_installer.core.api import ZoneSummary
from sl_installer.ui import naming


@pytest.mark.parametrize(
    "mac, expected",
    [
        ("ac:27:6e:a4:3d:30", "SLIC ATTENDANCE-3D31"),
        ("14:c1:9f:46:ae:1c", "SLIC ATTENDANCE-AE1D"),  # firmware evidence, setup_portal.c
        ("ac:27:6e:a3:16:d4", "SLIC ATTENDANCE-16D5"),
        ("AC276EA43DFF", "SLIC ATTENDANCE-3D00"),  # +1 on the last byte only, no carry
    ],
)
def test_hotspot_name(mac, expected):
    assert naming.hotspot_name(mac) == expected


def test_hotspot_name_needs_a_board_id():
    assert naming.hotspot_name("") is None
    assert naming.hotspot_name("not-a-mac") is None


def test_board_id_is_upper_case_with_colons():
    assert naming.board_id("ac276ea43d30") == "AC:27:6E:A4:3D:30"
    assert naming.board_id("ac:27:6e:a4:3d:30") == "AC:27:6E:A4:3D:30"


ZONE_NAMES = ["LF-ZONE-BLD9-01", "LF-ZONE-BLD9-02", "ZONE-LAHORE-01", "ZONE-LAHORE-02"]


@pytest.mark.parametrize(
    "code, others, name, group",
    [
        ("LF-ZONE-BLD9-03", ZONE_NAMES, "Building 9 · Device 3", naming.LIFE),
        ("LF-ZONE-BLD9-03", [], "Building 9", naming.LIFE),  # one device in a place: no number
        ("lf_zone_bld5_01", [], "Building 5", naming.LIFE),
        ("LF ZONE BLD1 01", ["LF-ZONE-BLD1-02"], "Building 1 · Device 1", naming.LIFE),
        ("LF-ZONE-G&PBLD8-01", [], "Group & Pension Building 8", naming.LIFE),
        ("ZONE-SLICTOWER-13FL", [], "SLIC Tower · 13th Floor", naming.HEALTH),
        ("ZONE-SLICTOWER-3FL", [], "SLIC Tower · 3rd Floor", naming.HEALTH),
        ("ZONE-LAHORE-01", ZONE_NAMES, "Lahore · Device 1", naming.HEALTH),
        ("ZONE-MULTAN-01", ZONE_NAMES, "Multan", naming.HEALTH),
        (
            "LFZONE-QUETTA-01",
            [],
            "Lfzone Quetta",
            naming.HEALTH,
        ),  # LF must be followed by -, _ or a space
        (
            "ZONE-BLD9-01",
            [],
            "Building 9",
            naming.HEALTH,
        ),  # no LF prefix: Health, as on the status page
    ],
)
def test_public_name_mirrors_the_status_page(code, others, name, group):
    assert naming.public_name(code, others) == name
    assert naming.group_of(code) == group


def test_public_name_does_not_count_itself_twice():
    assert naming.public_name("ZONE-MULTAN-01", ["ZONE-MULTAN-01"]) == "Multan"


@pytest.mark.parametrize(
    "value, ok",
    [
        ("LF-ZONE-BLD9-03", True),
        ("ZONE_SWAT.01", True),
        ("", False),
        ("LF ZONE 9", False),
        ("-ZONE", False),
        ("LF-ZONE-G&PBLD8-01", False),  # the provisioning validators reject '&' today
        ("A" * 64, True),
        ("A" * 65, False),
    ],
)
def test_zone_id_check(value, ok):
    assert (naming.zone_id_problem(value) is None) is ok


def test_zone_id_spaces_get_their_own_words():
    assert "spaces" in naming.zone_id_problem("LF ZONE 9")


@pytest.mark.parametrize(
    "value, ok",
    [
        ("LF-ZONE-BLD9-03", True),
        ("Lahore office", True),
        (" leading", False),
        ("trailing ", False),
        ("", False),
        ("x" * 121, False),
        ("bad\x07bell", False),
    ],
)
def test_zone_name_check(value, ok):
    assert (naming.zone_name_problem(value) is None) is ok


@pytest.mark.parametrize(
    "value, ok",
    [
        ("test-wifi-pass", True),
        ("1234567", False),
        ("x" * 63, True),
        ("x" * 64, False),
        ("ab" * 32, True),  # a 64-hex key
        ("", False),
    ],
)
def test_wifi_password_check(value, ok):
    assert (naming.wifi_password_problem(value) is None) is ok


@pytest.mark.parametrize(
    "value, ok",
    [
        ("0.0.0.0", True),
        ("192.168.1.20", True),
        ("10.1.2.3", True),
        ("172.16.0.1", True),
        ("172.32.0.1", False),
        ("8.8.8.8", False),
        ("192.168.1", False),
        ("", False),
    ],
)
def test_preferred_ip_check(value, ok):
    assert (naming.preferred_ip_problem(value) is None) is ok


def test_machine_ip_must_be_an_office_address():
    assert naming.machine_ip_problem("192.168.1.20") is None
    assert naming.machine_ip_problem("0.0.0.0") is not None
    assert naming.machine_ip_problem("") is not None


def test_numbers():
    assert naming.port_problem("4370") is None
    assert naming.port_problem("0") is not None
    assert naming.port_problem("70000") is not None
    assert naming.comm_key_problem("0") is None
    assert naming.comm_key_problem("4294967295") is None
    assert naming.comm_key_problem("4294967296") is not None
    assert naming.comm_key_problem("12a") is not None


def test_next_free_device_number():
    zones = [
        ZoneSummary("ZONE-LAHORE-01", "ZONE-LAHORE-01", ("1", "2")),
        ZoneSummary("ZONE-MULTAN-01", "ZONE-MULTAN-01", ("1", "3", "x")),
    ]
    assert naming.next_free_device_id("zone-lahore-01", zones) == "3"
    assert naming.next_free_device_id("ZONE-MULTAN-01", zones) == "2"
    assert naming.next_free_device_id("ZONE-NEW-01", zones, "1") == "1"


def test_app_version_gate():
    assert naming.app_is_too_old("1.0.0", "1.2.0")
    assert not naming.app_is_too_old("1.2.0", "1.2.0")
    assert not naming.app_is_too_old("1.10.0", "1.9.9")
    assert not naming.app_is_too_old("1.0.0", "")
