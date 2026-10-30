"""Check the secure-pull protocol (zkt_secure.c) with ESP-IDF's pinned mbedTLS.

Every value the firmware computes is compared with an independent Python
implementation, and the checker plays the terminal's side of a full handshake
(a 1024-bit PKCS#1 terminal key like the SenseFace M2F-LR's, then an SPKI one).
"""
from pathlib import Path
import base64
import hashlib
import hmac
import os
import shutil
import struct
import subprocess
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
SOURCE = Path(os.environ["IDF_PATH"]) / "components/mbedtls/mbedtls"
# SHA-256 of the 2033-byte DMC keystream in fandbsoft/zkteco4730-java (MIT).
KEYSTREAM_SHA256 = "77b8046423dd368826cb5c7506731e2b42b732ea25f19927cd8b0a2a0af1af84"

TABLE = []
for index in range(256):
    value = index
    for _ in range(8):
        value = (0xEDB88320 ^ (value >> 1)) if value & 1 else value >> 1
    TABLE.append(value)


def crc(data):
    value = 0xFFEEBBAA
    for byte in data:
        value = (value >> 8) ^ TABLE[(byte ^ value) & 0xFF]
    return value


def dmc(kind, body):
    return bytes([0xAA, 0xBB, kind]) + struct.pack("<H", len(body)) + body + struct.pack("<I", crc(body))


def undmc(plain):
    assert plain[:2] == b"\xaa\xbb"
    length = struct.unpack("<H", plain[3:5])[0]
    body = plain[5:5 + length]
    assert struct.unpack("<I", plain[5 + length:9 + length])[0] == crc(body)
    return plain[2], body


def aes(key, data, encrypt):
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    worker = cipher.encryptor() if encrypt else cipher.decryptor()
    return worker.update(data) + worker.finalize()


def derive(client, server):
    text = f"{client ^ server}.{((server << 8) & 0xFFFFFFFF) ^ client}.{((client << 8) & 0xFFFFFFFF) ^ server}"
    return text.encode()[:32].ljust(32, b"\0")


def native(comm_key, session, tick):
    key = 0
    for bit in range(32):
        key = (key << 1) | ((comm_key >> bit) & 1)
    key = (key + session) & 0xFFFFFFFF
    b1, b2, b3 = ((key >> 8) & 0xFF) ^ 0x4B, ((key >> 16) & 0xFF) ^ 0x53, ((key >> 24) & 0xFF) ^ 0x4F
    return bytes([b2 ^ tick, b3 ^ tick, tick, b1 ^ tick])


def auth_ext(comm_key):
    seed = hmac.new(b"ZKTeco-secret-key", b"StandalongComm", hashlib.sha256).hexdigest().encode()
    text = hashlib.md5(str(comm_key).encode()).hexdigest().encode()
    return hmac.new(seed, text, hashlib.sha256).digest() + bytes([0xB3, 0xF6, 0x00, 0x01])


def device_frame(key, zk, magic=0xAA, zero_crc=False):
    inner = bytes([magic, 0xBB]) + struct.pack("<I", len(zk)) + zk + struct.pack("<I", 0 if zero_crc else crc(zk))
    inner += bytes(-len(inner) % 16)
    sealed = aes(key, inner, True)
    return b"\x50\x50\x83\x7c" + struct.pack("<I", len(sealed)) + sealed


with tempfile.TemporaryDirectory(prefix="zkt-secure-pull-") as temporary:
    build = Path(temporary) / "mbedtls"
    subprocess.run(["cmake", "-S", str(SOURCE), "-B", str(build), "-DENABLE_TESTING=OFF",
                    "-DENABLE_PROGRAMS=OFF", "-DCMAKE_BUILD_TYPE=Debug",
                    "-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer"], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(build), "--parallel", "2"], check=True, stdout=subprocess.DEVNULL)
    binary = Path(temporary) / "secure-pull"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    "-I", str(SOURCE / "include"), str(ROOT / "tests/firmware/zkt_secure_host.c"),
                    str(MAIN / "zkt_secure.c"), str(build / "library/libmbedcrypto.a"), "-o", str(binary)],
                   check=True)
    harness = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def read(name):
        line = harness.stdout.readline().strip()
        assert line.startswith(name + "="), (name, line[:80])
        return line[len(name) + 1:]

    def send(name, data):
        harness.stdin.write(f"{name}={data.hex()}\n")
        harness.stdin.flush()

    keystream = bytes.fromhex(read("keystream"))
    assert len(keystream) == 2033 and hashlib.sha256(keystream).hexdigest() == KEYSTREAM_SHA256
    assert int(read("crc"), 16) == crc(bytes(range(256)))
    for client, server in ((1761811200, 3735928559), (1, 1), (4294967295, 0), (0, 4294967295)):
        assert bytes.fromhex(read("derive")) == derive(client, server)
    for index, comm_key in enumerate((197911, 0, 1979, 4294967295)):
        assert bytes.fromhex(read("native")) == native(comm_key, 22761 + index * 9000, 0x5A + index)
        assert bytes.fromhex(read("auth_ext")) == auth_ext(comm_key)

    key = bytes(range(0xA0, 0xC0))
    sealed = bytes.fromhex(read("sealed"))
    assert sealed[:4] == b"\x50\x50\x83\x7c" and struct.unpack("<I", sealed[4:8])[0] == len(sealed) - 8
    inner = aes(key, sealed[8:], False)
    zk = bytes([0xE8, 0x03, 0, 0, 0x11, 0x22, 0x01, 0]) + b"SDKBuild=1\0optio"
    assert inner[:2] == b"\xab\xbb" and struct.unpack("<I", inner[2:6])[0] == len(zk)
    assert inner[6:6 + len(zk)] == zk and struct.unpack("<I", inner[6 + len(zk):10 + len(zk)])[0] == crc(zk)
    assert inner[10 + len(zk):] == bytes(len(inner) - 10 - len(zk))

    reply = bytes([0xD0, 0x07, 0, 0, 0x11, 0x22, 0x01, 0]) + b"~SerialNumber=UFS2253100068\0"
    send("device_frame", device_frame(key, reply, zero_crc=True))
    assert bytes.fromhex(read("opened")) == reply
    bad = bytearray(device_frame(key, reply))
    bad[-1] ^= 0xFF
    send("bad_frame", bytes(bad))

    _, client_pem = undmc(bytes(a ^ b for a, b in zip(base64.b64decode(read("client_dmc")), keystream)))
    assert client_pem.startswith(b"-----BEGIN RSA PUBLIC KEY-----\n")
    assert client_pem.endswith(b"\n-----END RSA PUBLIC KEY-----\n")
    assert all(len(line) <= 64 for line in client_pem.splitlines())
    client_key = serialization.load_pem_public_key(client_pem)
    assert client_key.key_size == 2048 and client_key.public_numbers().e == 65537

    for round_index, public_format in enumerate((serialization.PublicFormat.PKCS1,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)):
        terminal = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        terminal_pem = terminal.public_key().public_bytes(serialization.Encoding.PEM, public_format)
        message = dmc(1, terminal_pem + b"\0")
        # The terminal pads its base64 reply with NUL bytes.
        send("device_reply", base64.b64encode(bytes(a ^ b for a, b in zip(message, keystream))) + b"\0\0")
        kind, secret = undmc(terminal.decrypt(base64.b64decode(read("key_exchange")), padding.PKCS1v15()))
        assert kind == 2 and struct.unpack("<I", secret)[0] == 0x01020304 + round_index
        server_secret = 0x9E3779B9 ^ round_index
        encrypted = client_key.encrypt(dmc(2, struct.pack("<I", server_secret)), padding.PKCS1v15())
        send("server_reply", base64.b64encode(encrypted) + b"\0")
        assert int(read("server_secret")) == server_secret
    send("garbage_reply", b"bm90IGEgdmFsaWQgcmVwbHk=")
    assert harness.stdout.readline().strip() == "done"
    assert harness.wait(timeout=60) == 0

print("Secure-pull keystream, DMC, CRC, AES framing, comm keys and a full simulated handshake passed")
