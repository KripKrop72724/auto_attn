"""Checks for the settings a person types, matching what head office will accept.

The rules copy firmware/zone_lite/tools/build_provisioning_package.py
(validate_request) and provision_zone_lite.py (nvs_rows), so a person learns about
a mistake on the Details screen instead of after a round trip. The server stays
authoritative.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import date
from typing import List, Optional, Tuple

from . import texts
from .api import Advice, Family, HIK_MODELS, HikSettings, InstallConfig, InstallMode, ZktSettings

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUEST_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
ZONE_ID_MAX = 64
DEVICE_ID_MAX = 31
ZONE_NAME_MAX = 120
SSID_MAX = 32
# The setup portal and the firmware's Wi-Fi save accept 8-63 characters; the package
# builder also takes a 64-hex key.
WIFI_PASSWORD_MIN, WIFI_PASSWORD_MAX = 8, 63
HIK_LIMITS = {
    "hik_host": 63, "hik_username": 63, "hik_password": 127,
    "hik_expected_serial": 119, "hik_profile": 79, "hik_source_epoch": 64,
}
# ESP-IDF 5.5.3 nvs_partition_gen refuses strings over 4000 bytes, below the 4095
# the other validators allow.
HIK_CA_MAX = 4000
HIK_TRANSPORTS = ("http_digest", "https")
UNKNOWN_MODEL_PROFILE = "hikvision-other-unverified-v1"


def _bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def version_tuple(version: str) -> Tuple[int, ...]:
    """(2, 7, 8) for "2.7.8"; raises ValueError for anything else."""
    return tuple(int(part) for part in version.split("."))


def check_zone_id(value: str) -> Optional[Advice]:
    if not value or _bytes(value) > ZONE_ID_MAX or not IDENTIFIER.fullmatch(value):
        return texts.invalid_field("zone_id")
    return None


def check_device_id(value: str) -> Optional[Advice]:
    if not value or _bytes(value) > DEVICE_ID_MAX or not IDENTIFIER.fullmatch(value):
        return texts.invalid_field("device_id")
    return None


def check_zone_name(value: str) -> Optional[Advice]:
    if (not value or _bytes(value) > ZONE_NAME_MAX or value != value.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        return texts.invalid_field("zone_name")
    return None


def check_wifi(ssid: str, password: str) -> Optional[Advice]:
    if not ssid or _bytes(ssid) > SSID_MAX or "\x00" in ssid:
        return texts.invalid_field("wifi_ssid")
    if not (WIFI_PASSWORD_MIN <= _bytes(password) <= WIFI_PASSWORD_MAX
            or HEX64.fullmatch(password)) or "\x00" in password:
        return texts.invalid_field("wifi_password")
    return None


_RFC1918 = tuple(ipaddress.ip_network(net)
                 for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_lan_ipv4(value: str) -> bool:
    """A private (RFC 1918) IPv4 address, the only kind ADD accepts for a machine."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and any(address in net for net in _RFC1918)


def check_zkt(settings: ZktSettings) -> Optional[Advice]:
    if type(settings.port) is not int or not 1 <= settings.port <= 65535:
        return texts.invalid_field("zkt_port")
    if type(settings.comm_key) is not int or not 0 <= settings.comm_key <= 0xFFFFFFFF:
        return texts.invalid_field("zkt_comm_key")
    preferred = settings.preferred_ip
    if preferred != "0.0.0.0" and not is_lan_ipv4(preferred):
        return texts.invalid_field("zkt_preferred_ip")
    return None


def check_hik(settings: HikSettings) -> Optional[Advice]:
    if not is_lan_ipv4(settings.host):
        return texts.invalid_field("hik_host")
    if type(settings.port) is not int or not 1 <= settings.port <= 65535:
        return texts.invalid_field("hik_port")
    if settings.transport not in HIK_TRANSPORTS:
        return texts.invalid_field("hik_transport")
    for field, value in (("hik_username", settings.username),
                         ("hik_password", settings.password),
                         ("hik_expected_serial", settings.expected_serial)):
        if not value or _bytes(value) > HIK_LIMITS[field] or "\x00" in value:
            return texts.invalid_field(field)
    if settings.profile and (_bytes(settings.profile) > HIK_LIMITS["hik_profile"]
                             or "\x00" in settings.profile):
        return texts.invalid_field("hik_profile")
    if settings.source_epoch and (_bytes(settings.source_epoch) > HIK_LIMITS["hik_source_epoch"]
                                  or "\x00" in settings.source_epoch):
        return texts.invalid_field("hik_source_epoch")
    if _bytes(settings.ca_pem) > HIK_CA_MAX or "\x00" in settings.ca_pem:
        return texts.invalid_field("hik_ca_pem")
    return None


def problems(config: InstallConfig, mode: Optional[InstallMode] = None) -> List[Advice]:
    """Every problem with ``config``, in the order the Details screen shows fields.

    A settings change writes no board software, so only it may come without a bundle.
    """
    found: List[Optional[Advice]] = [
        check_zone_id(config.zone_id),
        check_zone_name(config.zone_name),
        check_device_id(config.device_id),
        check_wifi(config.wifi_ssid, config.wifi_password),
    ]
    if not config.bundle_id and mode is not InstallMode.CHANGE_SETTINGS:
        found.append(texts.invalid_field("bundle_id"))
    if config.family is Family.ZKT:
        found.append(check_zkt(config.zkt or ZktSettings()))
    elif config.hik is None:
        found.append(texts.invalid_field("hik_host"))
    else:
        found.append(check_hik(config.hik))
    return [advice for advice in found if advice is not None]


def hik_profile_for(settings: HikSettings) -> str:
    """The profile string for the chosen model, unless one was typed."""
    if settings.profile:
        return settings.profile
    for model in HIK_MODELS:
        if model.key == settings.model_key and model.profile:
            return model.profile
    return UNKNOWN_MODEL_PROFILE


def default_source_epoch(zone_id: str, today: date, known_epoch: Optional[str] = None) -> str:
    """``<zone_id lower>-<yyyymmdd>``, or the epoch ADD already holds for this board.

    Keeping ADD's epoch matters: a new epoch stops attendance delivery until head
    office reviews the Hikvision policy again.
    """
    if known_epoch:
        return known_epoch
    suffix = "-" + today.strftime("%Y%m%d")
    stem = zone_id.strip().lower()[: HIK_LIMITS["hik_source_epoch"] - len(suffix)]
    return stem + suffix
