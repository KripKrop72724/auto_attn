"""Validate the separate USB rescue role before the protected signer runs."""
import argparse
import hashlib
import json
import struct
from pathlib import Path


def application_digest(path: Path, project: str, version: str) -> str:
    data = path.read_bytes()
    if (len(data) < 288 or data[0] != 0xE9 or data[23] != 1
            or struct.unpack_from('<H', data, 12)[0] != 9
            or struct.unpack_from('<I', data, 32)[0] != 0xABCD5432
            or data[48:80].split(b'\0')[0] != version.encode()
            or data[80:112].split(b'\0')[0] != project.encode()):
        raise ValueError('Unexpected ESP32-S3 application identity')
    offset = 24
    for _ in range(data[1]):
        if offset + 8 > len(data):
            raise ValueError('Truncated segment')
        length = struct.unpack_from('<I', data, offset + 4)[0]
        offset += 8 + length
        if offset > len(data):
            raise ValueError('Truncated segment data')
    digest_offset = (offset + 16) // 16 * 16
    digest = hashlib.sha256(data[:digest_offset]).digest()
    if data[digest_offset:digest_offset + 32] != digest:
        raise ValueError('Application validation digest mismatch')
    if len(data) > 0x280000 - 0x1000:
        raise ValueError('Application leaves insufficient signature space')
    return digest.hex()


def validate_partitions(path: Path) -> None:
    data = path.read_bytes()
    expected = [
        (1, 2, 0x11000, 0x6000, 'nvs'),
        (1, 0, 0x17000, 0x2000, 'otadata'),
        (1, 1, 0x19000, 0x1000, 'phy_init'),
        (0, 0, 0x20000, 0x280000, 'factory'),
        (0, 16, 0x2a0000, 0x280000, 'ota_0'),
        (0, 17, 0x520000, 0x280000, 'ota_1'),
        (1, 130, 0x7a0000, 0x800000, 'storage'),
    ]
    for i, fields in enumerate(expected):
        magic, kind, sub, start, size, label, flags = struct.unpack_from('<HBBII16sI', data, i*32)
        if (magic != 0x50aa or flags or
                (kind, sub, start, size, label.split(b'\0')[0].decode()) != fields):
            raise ValueError('Unexpected recovery partition layout')
    offset = len(expected)*32
    if data[offset:offset+2] != b'\xeb\xeb' or data[offset+16:offset+32] != hashlib.md5(data[:offset]).digest():
        raise ValueError('Partition table digest mismatch')
    if any(v != 255 for v in data[offset+32:]):
        raise ValueError('Unexpected extra partition')


def validate_package(directory: Path, role: str) -> dict:
    project, version = {'usb-wifi-rescue': ('zone_wifi_rescue', '0.1.0'),
                        'usb-wifi-rescue-seed': ('wifi_rescue_seed', '0.0.1')}[role]
    digest = application_digest(directory / 'zone_lite.bin', project, version)
    validate_partitions(directory / 'partition-table.bin')
    sdk = set((directory / 'sdkconfig').read_text().splitlines())
    required = {
        'CONFIG_SECURE_BOOT=y', 'CONFIG_SECURE_BOOT_V2_ENABLED=y',
        'CONFIG_SECURE_SIGNED_ON_UPDATE=y', 'CONFIG_NVS_ENCRYPTION=y',
        'CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y', 'CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0',
        'CONFIG_PARTITION_TABLE_OFFSET=0x10000', 'CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y',
        'CONFIG_SECURE_ROM_DL_MODE_ENABLED=y', 'CONFIG_SECURE_ENABLE_SECURE_ROM_DL_MODE=y',
    }
    forbidden = {'CONFIG_SECURE_BOOT_ENABLE_AGGRESSIVE_KEY_REVOKE=y',
                 'CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y', 'CONFIG_SECURE_FLASH_ENC_ENABLED=y',
                 'CONFIG_APP_ANTI_ROLLBACK=y'}
    if not required <= sdk or forbidden & sdk:
        raise ValueError('Unexpected rescue security configuration')
    stack = next((line.split('=', 1)[1] for line in sdk
                  if line.startswith('CONFIG_ESP_MAIN_TASK_STACK_SIZE=')), '0')
    if not stack.isdecimal() or int(stack) < 8192:
        raise ValueError('Insufficient encrypted-NVS main task stack')
    image = (directory / 'zone_lite.bin').read_bytes()
    if b'SLIC_WIFI_RESCUE_UNSIGNED_TEST_ONLY' in image:
        raise ValueError('Unsigned test image must never be fleet signed')
    marker = b'SLIC_WIFI_RESCUE_START' if role == 'usb-wifi-rescue' else b'SLIC_RESCUE_SEED_BOOT'
    if marker not in image:
        raise ValueError('Role marker is missing')
    if (directory / 'ota_data_initial.bin').read_bytes() != b'\xff'*8192:
        raise ValueError('Unexpected initial OTA data')
    for name in ['bootloader.bin', 'flasher_args.json']:
        if not (directory / name).is_file():
            raise ValueError('Incomplete unsigned role package')
    return {'image_role': role, 'project': project, 'version': version,
            'application_sha256': digest, 'hil_only': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--role', choices=['usb-wifi-rescue', 'usb-wifi-rescue-seed'], required=True)
    args = parser.parse_args()
    print(json.dumps(validate_package(args.directory, args.role), sort_keys=True))
