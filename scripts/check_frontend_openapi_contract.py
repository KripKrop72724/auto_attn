"""Fail CI when safety-critical frontend contracts drift from FastAPI OpenAPI."""

from zk_add.web import app


def request_properties(spec: dict, path: str, method: str) -> set[str]:
    schema = spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    reference = schema["$ref"].rsplit("/", 1)[-1]
    return set(spec["components"]["schemas"][reference].get("properties", {}))


def main() -> None:
    spec = app.openapi()
    paths = spec["paths"]
    assert "get" in paths["/api/v1/alerts"]
    alert_parameters = {row["name"]: row for row in paths["/api/v1/alerts"]["get"]["parameters"]}
    assert {"state", "severity", "connector_id", "zone_id", "limit", "cursor"}.issubset(alert_parameters)
    assert alert_parameters["cursor"]["schema"].get("type") in {"string", None}
    assert "post" in paths["/api/v1/firmware/campaigns/preflight"]
    assert {"release_id", "zone_id"}.issubset(
        request_properties(spec, "/api/v1/firmware/campaigns/preflight", "post")
    )
    assert {"release_id", "zone_id", "reason", "typed_confirmation", "password", "scope_token", "idempotency_key"}.issubset(
        request_properties(spec, "/api/v1/firmware/campaigns", "post")
    )
    assert {"reason", "typed_confirmation"}.issubset(
        request_properties(spec, "/api/v2/devices/{connector_id}/users/{user_key}", "patch")
    )
    print("Frontend OpenAPI safety contracts are synchronized.")


if __name__ == "__main__":
    main()
