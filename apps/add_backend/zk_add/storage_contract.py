"""Signed storage compatibility rules; unknown formats fail closed."""

COMPAT_VERSION = "2.5.4"
CANDIDATE_VERSION = "2.6.0"
COMPAT_MARKER = "ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4"


def validate_storage_contract(manifest: dict, version: str) -> dict | None:
    contract = manifest.get("queue_storage")
    required = version in {COMPAT_VERSION, CANDIDATE_VERSION}
    if contract is None and not required:
        return None
    expected = {
        "schema_version": 1,
        "read_format": 2,
        "reader_mask": 63,
        "write_format": 2 if version == CANDIDATE_VERSION else 1,
        "compatibility_version": COMPAT_VERSION,
    }
    if not required or contract != expected or any(
        type(contract.get(key)) is not int for key in ("schema_version", "read_format", "reader_mask", "write_format")
    ):
        raise ValueError("Firmware storage contract is missing or unqualified.")
    if version == CANDIDATE_VERSION and manifest.get("minimum_bootstrap_version") != COMPAT_VERSION:
        raise ValueError("Segmented storage requires the compatibility predecessor.")
    return contract
