"""Fail CI when safety-critical frontend contracts drift from FastAPI OpenAPI."""

from zk_add.web import app


def request_schema(spec: dict, path: str, method: str) -> dict:
    schema = spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    # An optional body is anyOf [model, null].
    schema = next((item for item in schema.get("anyOf", [schema]) if "$ref" in item), schema)
    reference = schema["$ref"].rsplit("/", 1)[-1]
    return spec["components"]["schemas"][reference]


def request_properties(spec: dict, path: str, method: str) -> set[str]:
    return set(request_schema(spec, path, method).get("properties", {}))


def request_required(spec: dict, path: str, method: str) -> set[str]:
    return set(request_schema(spec, path, method).get("required", []))


def main() -> None:
    spec = app.openapi()
    paths = spec["paths"]
    assert "get" in paths["/api/v1/alerts"]
    alert_parameters = {row["name"]: row for row in paths["/api/v1/alerts"]["get"]["parameters"]}
    assert {"state", "severity", "connector_id", "zone_id", "queue", "limit", "cursor"}.issubset(alert_parameters)
    assert alert_parameters["cursor"]["schema"].get("type") in {"string", None}
    assert "post" in paths["/api/v1/firmware/campaigns/preflight"]
    assert {"release_id", "zone_id"}.issubset(
        request_properties(spec, "/api/v1/firmware/campaigns/preflight", "post")
    )
    # Firmware updates are one click: no reason, typed version or password is
    # required (an older open tab may still send them and they are ignored).
    one_click_fields = {"reason", "typed_confirmation", "password"}
    assert {"release_id", "zone_id", "scope_token", "idempotency_key"}.issubset(
        request_required(spec, "/api/v1/firmware/campaigns", "post")
    )
    assert {"override_stability_warnings", "stability_warnings_digest"}.issubset(
        request_properties(spec, "/api/v1/firmware/campaigns", "post")
    )
    assert not one_click_fields & request_required(spec, "/api/v1/firmware/campaigns", "post")
    assert {"release_id", "zone_ids", "idempotency_key"} == request_required(
        spec, "/api/v1/firmware/campaigns/bulk", "post"
    )
    bulk_zones = request_schema(spec, "/api/v1/firmware/campaigns/bulk", "post")["properties"]["zone_ids"]
    assert (bulk_zones.get("minItems"), bulk_zones.get("maxItems")) == (1, 30)
    assert {"deployment_id", "override_stability_warnings", "stability_warnings_digest",
            "resume_pending_deployments"}.issubset(
        request_properties(spec, "/api/v1/firmware/campaigns/{campaign_id}/retry-target", "post")
    )
    assert request_required(spec, "/api/v1/firmware/campaigns/{campaign_id}/retry-target", "post") == {
        "deployment_id"}
    assert not request_required(spec, "/api/v1/firmware/campaigns/{campaign_id}/{action}", "post")
    for path in ("/api/v1/firmware/campaigns/{campaign_id}/archive", "/api/v1/firmware/campaigns/archive-cancelled",
                 "/api/v1/firmware/campaigns/clear-history"):
        assert "post" in paths[path] and "requestBody" not in paths[path]["post"]
    campaign_list = {row["name"] for row in paths["/api/v1/firmware/campaigns"]["get"]["parameters"]}
    assert {"view", "status", "cursor", "limit", "include_archived"}.issubset(campaign_list)
    # Release revocation keeps its audited reason and step-up password.
    assert {"reason", "password"}.issubset(
        request_required(spec, "/api/v1/firmware/releases/{release_id}/revoke", "post")
    )
    retry_review = {row["name"] for row in paths["/api/v1/firmware/campaigns/{campaign_id}/retry-target"]["get"]
                    ["parameters"]}
    assert {"campaign_id", "deployment_id"}.issubset(retry_review)
    assert {"reason", "typed_confirmation"}.issubset(
        request_properties(spec, "/api/v2/devices/{connector_id}/users/{user_key}", "patch")
    )
    assert {"user_keys"}.issubset(
        request_properties(
            spec,
            "/api/v2/devices/{connector_id}/users/validate-selection",
            "post",
        )
    )
    assert {"reason", "password", "idempotency_key"}.issubset(
        request_properties(spec, "/api/v1/alerts/{alert_id}/resolve", "post")
    )
    assert {"expected_code", "reason", "password", "idempotency_key"}.issubset(
        request_properties(spec, "/api/v1/devices/{connector_id}/clear-error", "post")
    )
    assert {"zone_id", "reason", "typed_confirmation", "password", "idempotency_key"}.issubset(
        request_properties(spec, "/api/v2/attendance-identity-backlog/releases", "post")
    )
    assert {"password"}.issubset(
        request_properties(
            spec, "/api/v2/attendance-identity-backlog/releases/{job_id}/stop", "post"
        )
    )
    assert "get" in paths["/api/v2/attendance-identity-backlog/preview"]
    # The send dialog's explicit CNIC choices and "send now" confirmations.
    assert {"event_ids", "reason", "password", "idempotency_key", "cnic_choices", "send_now_event_ids"}.issubset(
        request_properties(spec, "/api/v2/attendance-direct-ords", "post")
    )
    assert {"event_ids"}.issubset(request_properties(spec, "/api/v2/attendance-direct-ords/preview", "post"))
    # Each CNIC choice is bound to the evidence the dialog showed, and a
    # punch-time CNIC another active user holds needs its own confirmation.
    choice = spec["components"]["schemas"]["DirectOrdsCnicChoice"]
    assert {"event_id", "choice", "evidence_digest", "held_by_other_confirmed"}.issubset(choice["properties"])
    assert {"event_id", "choice", "evidence_digest"}.issubset(choice["required"])
    assert "get" in paths["/api/v1/device-health/shadow"]
    assert {"connector_ids"}.issubset(request_properties(spec, "/api/v1/device-health/cleanup/preview", "post"))
    assert {"connector_ids", "digest", "expires_at", "signature", "alert_ids", "error_fix_connector_ids", "reason",
            "typed_confirmation", "password", "idempotency_key"}.issubset(
        request_properties(spec, "/api/v1/device-health/cleanup/apply", "post")
    )
    assert "get" in paths["/api/v1/checkin-notifications"]
    assert {"mode", "punch_policy", "pilot_zone_ids", "add_pilot_cnics", "remove_pilot_cnic_ids",
            "expected_revision", "reason", "password"}.issubset(
        request_properties(spec, "/api/v1/checkin-notifications/settings", "put")
    )
    assert {"reason", "password"}.issubset(
        request_properties(spec, "/api/v1/attendance/{event_id}/checkin-notification/retry", "post")
    )
    attendance_parameters = {row["name"] for row in paths["/api/v1/attendance"]["get"]["parameters"]}
    assert "notify_status" in attendance_parameters
    print("Frontend OpenAPI safety contracts are synchronized.")


if __name__ == "__main__":
    main()
