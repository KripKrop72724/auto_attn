"""Software updates read side: filters, attention-first order, plain row fields (UI/UX revamp ADV-17)."""

from __future__ import annotations

from datetime import timedelta

from test_firmware_one_click import (
    admin_client,
    campaign,
    db as db,
    plain_connector,
    plain_release,
    postgres_engine as postgres_engine,
)
from zk_add import firmware_list
from zk_add.time_utils import utc_now


def _fleet(db):
    release = plain_release(db)
    life = plain_connector(db, "lf-bld9-03")
    life.display_name, life.zone_id, life.zone_name = "LF-ZONE-BLD9-03", "LF-ZONE-BLD9-03", "Building Nine"
    health = plain_connector(db, "peshawar-02")
    health.display_name, health.zone_id, health.zone_name = "ZONE-PESHAWAR-02", "ZONE-PESHAWAR-02", "Peshawar"
    other = plain_connector(db, "swat-01")
    other.display_name, other.zone_id = "ZONE-SWAT-01", "ZONE-SWAT-01"
    db.flush()
    failed, _ = campaign(db, release, health.zone_id, status="PAUSED", deployments=[
        (health, "FAILED", True, None)])
    failed.pause_reason = f"{health.connector_id}: FAILED (OTA_DOWNLOAD_TIMEOUT)"
    one_click, _ = campaign(db, release, life.zone_id, status="PAUSED", deployments=[
        (life, "PENDING", False, None)])
    one_click.pause_reason = "Paused with one click"
    one_click.created_at = utc_now() - timedelta(days=10)
    waiting, waiting_rows = campaign(db, release, other.zone_id, status="ACTIVE", deployments=[
        (other, "PENDING", False, None)])
    waiting_rows[0].error_code = "DEVICE_OFFLINE"
    db.commit()
    return {"failed": failed, "one_click": one_click, "waiting": waiting, "life": life, "health": health}


def _ids(client, **params):
    response = client.get("/api/v1/firmware/campaigns", params={"view": "summary", **params})
    assert response.status_code == 200, response.text
    return [row["campaign_id"] for row in response.json()["rows"]]


def test_division_attention_waiting_dates_and_attention_sort(db):
    fleet = _fleet(db)
    client, _headers = admin_client(db)
    failed, one_click, waiting = (fleet[key].campaign_id for key in ("failed", "one_click", "waiting"))

    assert _ids(client, division="life") == [one_click]
    life_totals = client.get("/api/v1/firmware/campaigns", params={"division": "life"}).json()["totals"]
    assert life_totals["campaigns"]["all"] == 1
    assert set(_ids(client, division="health")) == {failed, waiting}
    assert _ids(client, attention="true") == [failed]
    assert _ids(client, waiting="true") == [waiting]
    since = (utc_now() - timedelta(days=2)).isoformat()
    assert set(_ids(client, created_from=since)) == {failed, waiting}
    assert _ids(client, created_to=since) == [one_click]
    # Needs attention first, then updating, then newest.
    assert _ids(client, sort="attention") == [failed, waiting, one_click]
    first = client.get("/api/v1/firmware/campaigns", params={"sort": "attention", "limit": 1}).json()
    assert [row["campaign_id"] for row in first["rows"]] == [failed]
    second = client.get("/api/v1/firmware/campaigns", params={
        "sort": "attention", "limit": 1, "cursor": first["next_cursor"]}).json()
    assert [row["campaign_id"] for row in second["rows"]] == [waiting]
    # Search also matches the zone's stored name.
    assert _ids(client, q="Building Nine") == [one_click]
    assert client.get("/api/v1/firmware/campaigns", params={"division": "x"}).status_code == 422


def test_campaign_rows_carry_place_division_and_pause_fields(db):
    fleet = _fleet(db)
    client, _headers = admin_client(db)
    rows = {row["campaign_id"]: row for row in client.get("/api/v1/firmware/campaigns").json()["rows"]}
    failed = rows[fleet["failed"].campaign_id]
    assert failed["division"] == "health"
    assert failed["place_name"] == "Peshawar · Device 2"
    assert failed["pause_kind"] == "DEVICE_FAILED"
    assert failed["pause_code"] == "OTA_DOWNLOAD_TIMEOUT"
    assert failed["pause_device_connector_id"] == fleet["health"].connector_id
    assert failed["pause_device_name"] == "Peshawar · Device 2"
    assert failed["pause_reason"] == f"{fleet['health'].connector_id}: FAILED (OTA_DOWNLOAD_TIMEOUT)"
    one_click = rows[fleet["one_click"].campaign_id]
    assert one_click["division"] == "life" and one_click["place_name"] == "Building 9 · Device 3"
    assert one_click["pause_kind"] == "ONE_CLICK" and one_click["pause_code"] is None
    assert rows[fleet["waiting"].campaign_id]["pause_kind"] is None


def test_pause_fields_read_every_stored_pause_reason():
    assert firmware_list.pause_fields("abc: ROLLED_BACK (UNKNOWN)", {"abc": "Swat"}) == {
        "pause_kind": "DEVICE_ROLLED_BACK", "pause_code": "UNKNOWN",
        "pause_device_connector_id": "abc", "pause_device_name": "Swat"}
    assert firmware_list.pause_fields("Release revoked: bad build", {})["pause_kind"] == "RELEASE_REVOKED"
    assert firmware_list.pause_fields("Something else", {})["pause_kind"] == "OTHER"
    assert firmware_list.pause_fields(None, {})["pause_kind"] is None


def test_release_family_filter(db):
    zkt = plain_release(db, "3.0.1")
    zkt.manifest = {**zkt.manifest, "firmware_family": "zkt"}
    hik = plain_release(db, "4.0.1")
    hik.manifest = {"application_sha256": "d" * 64, "firmware_family": "hikvision",
                    "project_name": "zone_lite_hikvision"}
    db.commit()
    client, _headers = admin_client(db)

    def versions(**params):
        response = client.get("/api/v1/firmware/releases", params=params)
        assert response.status_code == 200, response.text
        return sorted(row["version"] for row in response.json()["rows"])

    assert versions(family="hikvision") == ["4.0.1"]
    assert versions(family="zkt") == ["3.0.1"]
    assert versions() == ["3.0.1", "4.0.1"]
    assert client.get("/api/v1/firmware/releases", params={"family": "other"}).status_code == 422
