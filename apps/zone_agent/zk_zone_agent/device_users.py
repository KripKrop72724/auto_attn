from __future__ import annotations

from dataclasses import dataclass


PRIVILEGE_REGULAR = 0
PRIVILEGE_ADMIN = 14
PRIVILEGE_CHOICES = {
    PRIVILEGE_REGULAR: "Regular User",
    PRIVILEGE_ADMIN: "Admin",
}
MAX_USER_ID_LENGTH = 24
MAX_NAME_BYTES = 24
MAX_CARD_VALUE = 4_294_967_295


@dataclass(frozen=True)
class DeviceUserUpdate:
    uid: str
    user_id: str
    name: str
    privilege: int
    card: int


def normalize_device_user_update(
    *,
    uid: str,
    user_id: str,
    name: str,
    privilege: str | int,
    card: str | int | None,
) -> DeviceUserUpdate:
    uid = uid.strip()
    user_id = user_id.strip()
    name = " ".join(name.strip().split())

    if not uid:
        raise ValueError(
            "Refresh this device before editing users; the local row is missing a device UID."
        )
    if not uid.isdigit():
        raise ValueError("Device UID must be numeric.")
    if not user_id:
        raise ValueError("User ID is required.")
    if not user_id.isdigit():
        raise ValueError("User ID must contain digits only.")
    if len(user_id) > MAX_USER_ID_LENGTH:
        raise ValueError(f"User ID must be {MAX_USER_ID_LENGTH} digits or fewer.")
    if not name:
        raise ValueError("Name is required.")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"Name must fit within {MAX_NAME_BYTES} UTF-8 bytes for the device.")

    try:
        privilege_int = int(privilege)
    except (TypeError, ValueError) as exc:
        raise ValueError("Privilege is invalid.") from exc
    if privilege_int not in PRIVILEGE_CHOICES:
        raise ValueError("Privilege must be Regular User or Admin.")

    card_value = 0
    if card not in (None, ""):
        try:
            card_value = int(str(card).strip())
        except ValueError as exc:
            raise ValueError("Card number must be numeric.") from exc
        if card_value < 0 or card_value > MAX_CARD_VALUE:
            raise ValueError("Card number is outside the supported device range.")

    return DeviceUserUpdate(
        uid=uid,
        user_id=user_id,
        name=name,
        privilege=privilege_int,
        card=card_value,
    )
