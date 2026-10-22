import hashlib
import struct

import pytest

from scripts.usb_wifi_rescue_artifacts import (
    application_digest,
    validate_package,
    validate_partitions,
)


def image(project=b'zone_wifi_rescue', version=b'0.1.0', marker=b''):
    header = bytearray(24)
    header[0] = 0xe9
    header[1] = 1
    header[23] = 1
    struct.pack_into('<H', header, 12, 9)
    descriptor = bytearray(256)
    struct.pack_into('<I', descriptor, 0, 0xabcd5432)
    descriptor[16:16+len(version)] = version
    descriptor[48:48+len(project)] = project
    descriptor[160:160+len(marker)] = marker
    data = header + struct.pack('<II', 0x3c000020, len(descriptor)) + descriptor
    data += b'\0' * (16 - len(data) % 16)
    return data + hashlib.sha256(data).digest()


def test_role_image_identity_and_digest_fail_closed(tmp_path):
    path = tmp_path / 'app.bin'
    valid = image()
    path.write_bytes(valid)
    assert len(application_digest(path, 'zone_wifi_rescue', '0.1.0')) == 64
    for data in [image(b'zone_lite'), image(version=b'2.7.6'), valid[:-1], bytes(400)]:
        path.write_bytes(data)
        with pytest.raises(ValueError):
            application_digest(path, 'zone_wifi_rescue', '0.1.0')
    bad = bytearray(valid)
    bad[150] ^= 1
    path.write_bytes(bad)
    with pytest.raises(ValueError, match='digest mismatch'):
        application_digest(path, 'zone_wifi_rescue', '0.1.0')


def partition_table():
    rows = [(1, 2, 0x11000, 0x6000, 'nvs'), (1, 0, 0x17000, 0x2000, 'otadata'),
            (1, 1, 0x19000, 0x1000, 'phy_init'), (0, 0, 0x20000, 0x280000, 'factory'),
            (0, 16, 0x2a0000, 0x280000, 'ota_0'), (0, 17, 0x520000, 0x280000, 'ota_1'),
            (1, 130, 0x7a0000, 0x800000, 'storage')]
    body = b''.join(struct.pack('<HBBII16sI', 0x50aa, k, s, a, n, label.encode(), 0)
                    for k, s, a, n, label in rows)
    return body + b'\xeb\xeb' + b'\xff'*14 + hashlib.md5(body).digest() + b'\xff'*128


def test_partition_layout_rejects_attendance_or_original_slot_changes(tmp_path):
    table = partition_table()
    path = tmp_path / 'table.bin'
    path.write_bytes(table)
    validate_partitions(path)
    for offset in [5*32+4, 6*32+4, 6*32+8]:
        altered = bytearray(table)
        altered[offset] ^= 1
        path.write_bytes(altered)
        with pytest.raises(ValueError, match='partition layout'):
            validate_partitions(path)


def test_signing_validation_rejects_missing_hmac_security_and_unsigned_variant(tmp_path):
    (tmp_path / 'zone_lite.bin').write_bytes(image(marker=b'SLIC_WIFI_RESCUE_START'))
    (tmp_path / 'partition-table.bin').write_bytes(partition_table())
    (tmp_path / 'ota_data_initial.bin').write_bytes(b'\xff'*8192)
    (tmp_path / 'bootloader.bin').write_bytes(b'synthetic fixture; not flashable')
    (tmp_path / 'flasher_args.json').write_text('{}')
    config = ('CONFIG_SECURE_BOOT=y\nCONFIG_SECURE_BOOT_V2_ENABLED=y\n'
              'CONFIG_SECURE_SIGNED_ON_UPDATE=y\nCONFIG_NVS_ENCRYPTION=y\n'
              'CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y\nCONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0\n'
              'CONFIG_PARTITION_TABLE_OFFSET=0x10000\nCONFIG_ESPTOOLPY_FLASHSIZE_16MB=y\n'
              'CONFIG_SECURE_ROM_DL_MODE_ENABLED=y\nCONFIG_SECURE_ENABLE_SECURE_ROM_DL_MODE=y\n'
              'CONFIG_ESP_MAIN_TASK_STACK_SIZE=16384')
    (tmp_path / 'sdkconfig').write_text(config)
    for size in ['3584', '0', 'invalid']:
        (tmp_path / 'sdkconfig').write_text(config.replace('STACK_SIZE=16384', 'STACK_SIZE=' + size))
        with pytest.raises(ValueError, match='main task stack'):
            validate_package(tmp_path, 'usb-wifi-rescue')
    (tmp_path / 'sdkconfig').write_text(config)
    assert validate_package(tmp_path, 'usb-wifi-rescue')['hil_only']
    for bad in [config.replace('CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0', 'CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=1'),
                config.replace('CONFIG_SECURE_SIGNED_ON_UPDATE=y',''),
                config + '\nCONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y']:
        (tmp_path / 'sdkconfig').write_text(bad)
        with pytest.raises(ValueError, match='security configuration'):
            validate_package(tmp_path, 'usb-wifi-rescue')
    (tmp_path / 'sdkconfig').write_text(config)
    (tmp_path / 'zone_lite.bin').write_bytes(image(marker=b'SLIC_WIFI_RESCUE_UNSIGNED_TEST_ONLY'))
    with pytest.raises(ValueError, match='must never be fleet signed'):
        validate_package(tmp_path, 'usb-wifi-rescue')
