"""Generate one build-local, fail-closed ESP-IDF 5.5.3 AES cleanup correction.

The SDK itself is never modified. Upstream esp_aes_process_dma_ext_ram leaks
its input bounce buffer if the output allocation fails. Preserve its zeroize
and error behavior, releasing only the previously allocated input buffer.
"""

import argparse
import hashlib
from pathlib import Path

SOURCE_SHA256 = "a4aeeffeaabd0e3a37dbae2813ad5d98c0264dd65d43843e75e3ad8e831a69ac"
ORIGINAL = """        if (output_buf == NULL) {
            mbedtls_platform_zeroize(output, len);
            ESP_LOGE(TAG, "Failed to allocate memory");
            return -1;
        }"""
CORRECTED = ORIGINAL.replace("            return -1;", "            free(input_buf);\n            return -1;")


def patch_failure_path(source: str) -> str:
    if source.count(ORIGINAL) != 1:
        raise ValueError("AES cleanup source context does not match the reviewed patch")
    return source.replace(ORIGINAL, CORRECTED, 1)


def generate(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("AES cleanup must use a build-local copy, never the SDK source")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unknown ESP-IDF AES source; review the SDK before applying cleanup correction")
    corrected = patch_failure_path(data.decode("utf-8")).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Avoid changing a generated input's mtime on unchanged reconfiguration.
    if not destination.exists() or destination.read_bytes() != corrected:
        destination.write_bytes(corrected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    generate(args.source, args.destination)
