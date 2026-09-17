"""Check ESP application descriptor before a signer assigns a firmware family."""
import argparse
from pathlib import Path
import struct


def verify(path: Path, family: str, version: str | None = None) -> None:
    with path.open("rb") as stream:
        header = stream.read(112)
    if (len(header) != 112 or header[0] != 0xE9
            or struct.unpack_from("<I", header, 32)[0] != 0xABCD5432):
        raise ValueError("Invalid ESP application descriptor")
    expected = {"zkt": b"zone_lite", "hikvision": b"zone_lite_hikvision"}[family]
    if header[80:112].split(b"\0", 1)[0] != expected:
        raise ValueError("Application project does not match firmware family")
    if version is not None and header[48:80].split(b"\0", 1)[0] != version.encode("ascii"):
        raise ValueError("Application version does not match release version")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--family", required=True, choices=("zkt", "hikvision"))
    parser.add_argument("--version")
    args = parser.parse_args()
    verify(args.image, args.family, args.version)
