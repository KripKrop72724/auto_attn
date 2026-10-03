"""Exercise the ESP-IDF-pinned mbedTLS adapter against independent crypto vectors."""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import json

from custody_wire_vectors import canonical, digest, vectors

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
SOURCE = Path(os.environ["IDF_PATH"]) / "components/mbedtls/mbedtls"
CJSON = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"

with tempfile.TemporaryDirectory(prefix="zkt-journal-crypto-") as temporary:
    build = Path(temporary) / "mbedtls"
    subprocess.run(["cmake", "-S", str(SOURCE), "-B", str(build), "-DENABLE_TESTING=OFF",
                    "-DENABLE_PROGRAMS=OFF", "-DCMAKE_BUILD_TYPE=Debug",
                    "-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer"], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(build), "--parallel", "2"], check=True,
                   stdout=subprocess.DEVNULL)
    binary = Path(temporary) / "journal-crypto"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    "-I", str(SOURCE / "include"),
                    str(ROOT / "tests/firmware/zkt_journal_crypto_host.c"),
                    str(MAIN / "zkt_journal_codec.c"), str(MAIN / "zkt_journal_crypto.c"),
                    str(build / "library/libmbedcrypto.a"), "-o", str(binary)], check=True)
    result = subprocess.check_output([str(binary)], text=True)
    actual = bytes.fromhex(next(line.removeprefix("vector=") for line in result.splitlines()
                                if line.startswith("vector=")))
    wire_binary = Path(temporary) / "custody-wire"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer", "-I", str(MAIN), "-I", str(SOURCE / "include"),
                    "-I", str(CJSON), str(ROOT / "tests/firmware/zkt_custody_wire_host.c"),
                    str(MAIN / "zkt_custody_wire.c"), str(MAIN / "zkt_custody_receipt.c"),
                    str(MAIN / "zkt_journal_codec.c"),
                    str(MAIN / "zkt_journal_crypto.c"), str(CJSON / "cJSON.c"),
                    str(build / "library/libmbedcrypto.a"), "-lm", "-o", str(wire_binary)], check=True)
    lines = subprocess.check_output([str(wire_binary)], text=True).splitlines()
    wire = [line.removeprefix("wire=") for line in lines if line.startswith("wire=")]
    expected = [line.removeprefix("expected=").split(",") for line in lines if line.startswith("expected=")]
    proofs = [line.removeprefix("proof=") for line in lines if line.startswith("proof=")]
    assert len(wire) == len(expected) == len(proofs) == len(vectors())
    for value, encoded, identities, proof in zip(vectors(), wire, expected, proofs, strict=True):
        assert encoded == canonical({"observations": [value], "schema_version": 1})
        assert json.loads(encoded)["observations"][0] == value
        assert identities == [value["observation_id"], digest(value)]
        assert proof == digest(["zkt-add-custody-v1", *identities,
                                "11111111-2222-4333-8444-555555555555", "PRESERVED_UNRESOLVED"])

metadata = bytearray(240)
metadata[:8] = b"ZJ270S01"
struct.pack_into("<HH", metadata, 8, 1, 240)
struct.pack_into("<Q", metadata, 16, 9)
metadata[24:40] = bytes(range(16))
metadata[40:53] = b"TEST-TERMINAL"
metadata[121:126] = b"G3-v1"
metadata[186:187] = b"1"
serial = b"TEST-TERMINAL".ljust(81, b"\0")
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes(range(16)),
           info=b"ZKT-ATTENDANCE-JOURNAL-AES256GCM-V1" + serial).derive(bytes(range(32)))
header = struct.pack("<4sHBBQIHH", b"ZJO1", 120, 1, 2, 7, 0, 40, 0)
plain = struct.pack("<qQ16sII", 1700000000, 123456789, bytes(16), 2**32 - 1, 5) + bytes(range(40))
expected = header + AESGCM(key).encrypt(b"ZJ01" + struct.pack("<Q", 7), plain, bytes(metadata) + header)
assert actual == expected, "Journal encoding differs from independent HKDF/AES-GCM vector"
print("Pinned mbedTLS journal authentication, tamper tests, and independent crypto vector passed")
print("Canonical firmware custody payloads, 64-bit round trips, and receipt verification passed")
