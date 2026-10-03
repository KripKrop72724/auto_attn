"""Exercise the ESP-IDF-pinned mbedTLS adapter against independent crypto vectors."""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
SOURCE = Path(os.environ["IDF_PATH"]) / "components/mbedtls/mbedtls"

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
