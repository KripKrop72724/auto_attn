import pytest

from zk_zone_agent.device_users import normalize_device_user_update
from zk_zone_agent.zk_client import PyZKClient, _user_from_pyzk


class _RawUser:
    def __init__(
        self,
        *,
        uid=7,
        user_id="1007",
        name="Ali",
        privilege=0,
        password="2468",
        group_id="3",
        card=12345,
    ):
        self.uid = uid
        self.user_id = user_id
        self.name = name
        self.privilege = privilege
        self.password = password
        self.group_id = group_id
        self.card = card


def test_user_from_pyzk_includes_editable_and_hidden_fields():
    user = _user_from_pyzk(_RawUser())

    assert user.uid == "7"
    assert user.user_id == "1007"
    assert user.name == "Ali"
    assert user.privilege == "0"
    assert user.password == "2468"
    assert user.group_id == "3"
    assert user.card == 12345
    assert user.raw["card"] == 12345


def test_update_user_preserves_hidden_password_and_group_fields():
    raw = _RawUser()

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def get_users(self):
            return [raw]

        def set_user(self, **kwargs):
            self.calls.append(kwargs)
            raw.user_id = kwargs["user_id"]
            raw.name = kwargs["name"]
            raw.privilege = kwargs["privilege"]
            raw.card = kwargs["card"]

    conn = FakeConnection()
    client = PyZKClient(ip="192.168.1.20")
    client.conn = conn

    updated = client.update_user(uid="7", user_id="2007", name="Ali Khan", privilege=14, card=987)

    assert conn.calls == [
        {
            "uid": 7,
            "name": "Ali Khan",
            "privilege": 14,
            "password": "2468",
            "group_id": "3",
            "user_id": "2007",
            "card": 987,
        }
    ]
    assert updated.user_id == "2007"
    assert updated.name == "Ali Khan"
    assert updated.password == "2468"
    assert updated.group_id == "3"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("uid", "", "UID"),
        ("user_id", "", "User ID is required"),
        ("user_id", "A100", "digits only"),
        ("name", "", "Name is required"),
        ("privilege", "6", "Regular User or Admin"),
        ("card", "ABC", "numeric"),
    ],
)
def test_normalize_device_user_update_rejects_invalid_input(field, value, message):
    payload = {
        "uid": "7",
        "user_id": "1007",
        "name": "Ali Khan",
        "privilege": "0",
        "card": "",
    }
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        normalize_device_user_update(**payload)


def test_normalize_device_user_update_accepts_core_fields():
    update = normalize_device_user_update(
        uid="7",
        user_id="2007",
        name="  Ali   Khan  ",
        privilege="14",
        card="987654",
    )

    assert update.uid == "7"
    assert update.user_id == "2007"
    assert update.name == "Ali Khan"
    assert update.privilege == 14
    assert update.card == 987654
