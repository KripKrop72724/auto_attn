import pytest
from pydantic import ValidationError

from zk_add.schemas import HeartbeatPayload


def test_old_heartbeat_does_not_invent_running_image_evidence():
    payload = HeartbeatPayload.model_validate({"firmware_version": "2.4.12", "ota": {"capable": True}})
    assert payload.ota.running_version is None
    assert payload.ota.running_partition is None
    assert payload.ota.image_sha256 is None


def test_new_heartbeat_preserves_running_image_evidence():
    payload = HeartbeatPayload.model_validate({"ota": {
        "running_version": "2.6.0", "running_partition": "ota_1", "image_sha256": "a" * 64,
    }})
    evidence = payload.model_dump(mode="json")["ota"]
    assert evidence["running_version"] == "2.6.0"
    assert evidence["running_partition"] == "ota_1"
    assert evidence["image_sha256"] == "a" * 64


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_malformed_running_image_digest_is_rejected(digest):
    with pytest.raises(ValidationError):
        HeartbeatPayload.model_validate({"ota": {"image_sha256": digest}})
