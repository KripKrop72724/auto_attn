"""Signed storage compatibility rules; unknown formats fail closed."""

COMPAT_VERSION = "2.5.4"
CANDIDATE_VERSION = "2.6.0"
COMPAT_MARKER = "ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4"
DIRECT_VERSION = "2.6.1"
DIRECT_VERSIONS = (DIRECT_VERSION, "2.6.2", "2.6.3")
DIRECT_BASELINES = ("2.4.12", "2.5.2")
DIRECT_BASELINE_IMAGES = {
    "2.4.12": "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589",
    "2.5.2": "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
}
DIRECT_MARKER = "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2"


def validate_storage_contract(manifest: dict, version: str) -> dict | None:
    contract = manifest.get("queue_storage")
    required = version in {COMPAT_VERSION, CANDIDATE_VERSION, *DIRECT_VERSIONS}
    if contract is None and not required:
        return None
    expected = ({
        "schema_version": 2,
        "read_format": 2,
        "reader_mask": 63,
        "write_format": 1,
        "allowed_bootstrap_versions": list(DIRECT_BASELINES),
        "allowed_bootstrap_images": DIRECT_BASELINE_IMAGES,
    } if version in DIRECT_VERSIONS else {
        "schema_version": 1,
        "read_format": 2,
        "reader_mask": 63,
        "write_format": 2 if version == CANDIDATE_VERSION else 1,
        "compatibility_version": COMPAT_VERSION,
    })
    if not required or contract != expected or any(
        type(contract.get(key)) is not int for key in ("schema_version", "read_format", "reader_mask", "write_format")
    ):
        raise ValueError("Firmware storage contract is missing or unqualified.")
    if version == CANDIDATE_VERSION and manifest.get("minimum_bootstrap_version") != COMPAT_VERSION:
        raise ValueError("Segmented storage requires the compatibility predecessor.")
    if version in DIRECT_VERSIONS and manifest.get("minimum_bootstrap_version") != DIRECT_BASELINES[0]:
        raise ValueError("Direct legacy-write storage requires the qualified baseline.")
    return contract
