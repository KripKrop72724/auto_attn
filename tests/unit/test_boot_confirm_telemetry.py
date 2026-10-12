"""ADD accepts the ZKT 2.7.1 new-image confirmation progress."""
import pytest
from pydantic import ValidationError

from zk_add.schemas import HeartbeatPayload, OtaHeartbeatPayload


def test_confirmation_progress_is_accepted_and_older_firmware_omits_it():
    payload = HeartbeatPayload(ota={
        "state": "READY_TO_BOOT", "boot_health_checks": 40, "boot_confirm_active_seconds": 120,
        "boot_confirm_paused_seconds": 3600, "boot_confirm_elapsed_seconds": 3720})
    assert (payload.ota.boot_confirm_active_seconds, payload.ota.boot_confirm_paused_seconds,
            payload.ota.boot_confirm_elapsed_seconds) == (120, 3600, 3720)
    older = OtaHeartbeatPayload(state="READY_TO_BOOT", boot_health_checks=40)
    assert older.boot_confirm_active_seconds is None and older.boot_confirm_elapsed_seconds is None


@pytest.mark.parametrize("field", ["boot_confirm_active_seconds", "boot_confirm_paused_seconds",
                                   "boot_confirm_elapsed_seconds"])
@pytest.mark.parametrize("value", [-1, 2**32])
def test_confirmation_progress_is_bounded(field, value):
    with pytest.raises(ValidationError):
        OtaHeartbeatPayload(**{field: value})
