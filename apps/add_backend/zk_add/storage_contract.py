"""Signed storage compatibility rules; unknown formats fail closed."""

COMPAT_VERSION = "2.5.4"
CANDIDATE_VERSION = "2.6.0"
COMPAT_MARKER = "ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4"
DIRECT_VERSION = "2.6.1"
DIRECT_VERSIONS = (DIRECT_VERSION, "2.6.2", "2.6.3", "2.6.4", "2.6.5", "2.6.6", "2.6.7", "2.6.8", "2.6.9")
DIRECT_BASELINES = ("2.4.12", "2.5.2")
DIRECT_BASELINE_IMAGES = {
    "2.4.12": "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589",
    "2.5.2": "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
}
DIRECT_MARKER = "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2"
RETRY_VERSION = "2.6.7"
RETRY_BASELINES = (*DIRECT_BASELINES, "2.6.6")
RETRY_BASELINE_IMAGES = {
    **DIRECT_BASELINE_IMAGES,
    "2.6.6": "69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e",
}
RETRY_MARKER = "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6"
DIAGNOSTIC_VERSION = "2.6.8"
DIAGNOSTIC_BASELINES = (*RETRY_BASELINES, "2.6.7")
DIAGNOSTIC_BASELINE_IMAGES = {
    **RETRY_BASELINE_IMAGES,
    "2.6.7": "3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c",
}
DIAGNOSTIC_MARKER = "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7"
CONTENTION_VERSION = "2.6.9"
CONTENTION_BASELINES = (*DIAGNOSTIC_BASELINES, "2.6.8")
CONTENTION_BASELINE_IMAGES = {
    **DIAGNOSTIC_BASELINE_IMAGES,
    "2.6.8": "fecc5df0223a3c7c8b019a445bcf829bc8d09dd93e920aecc8446908fadeadc6",
}
CONTENTION_MARKER = "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8"


def validate_storage_contract(manifest: dict, version: str) -> dict | None:
    contract = manifest.get("queue_storage")
    required = version in {COMPAT_VERSION, CANDIDATE_VERSION, *DIRECT_VERSIONS}
    if contract is None and not required:
        return None
    direct_baselines = (CONTENTION_BASELINES if version == CONTENTION_VERSION else
                        DIAGNOSTIC_BASELINES if version == DIAGNOSTIC_VERSION else
                        RETRY_BASELINES if version == RETRY_VERSION else DIRECT_BASELINES)
    direct_images = (CONTENTION_BASELINE_IMAGES if version == CONTENTION_VERSION else
                     DIAGNOSTIC_BASELINE_IMAGES if version == DIAGNOSTIC_VERSION else
                     RETRY_BASELINE_IMAGES if version == RETRY_VERSION else DIRECT_BASELINE_IMAGES)
    expected = ({
        "schema_version": 2,
        "read_format": 2,
        "reader_mask": 63,
        "write_format": 1,
        "allowed_bootstrap_versions": list(direct_baselines),
        "allowed_bootstrap_images": direct_images,
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
