"""Unsigned, MAC-bound Wi-Fi rescue experiment with verified full restoration.

This is not a production installer or Secure Download acceptance receipt.
Only public synthetic credentials are used. No eFuse operation is available.
"""
import argparse
import hashlib
import json
import runpy
import struct
import time
import zlib
from pathlib import Path

import esptool
import serial

ROOT = Path(__file__).resolve().parents[4]
H = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'wifi_portal/run_spare.py'))
A = runpy.run_path(str(ROOT / 'scripts/usb_wifi_rescue_artifacts.py'))


def capture(port_name, destination, marker, timeout=45):
    data = bytearray()
    deadline = time.monotonic() + timeout
    with destination.open('wb') as output:
        while time.monotonic() < deadline:
            try:
                port = serial.Serial(port=None, baudrate=115200, timeout=0.5)
                port.dtr = port.rts = False
                port.port = port_name
                port.open()
                with port:
                    while time.monotonic() < deadline:
                        chunk = port.read(port.in_waiting or 1)
                        if chunk:
                            output.write(chunk)
                            output.flush()
                            data.extend(chunk)
                            if marker in data:
                                return bytes(data)
            except serial.SerialException:
                time.sleep(0.25)
    raise RuntimeError('Expected application evidence absent; inspect saved serial log')


def write_verified(esp, images):
    esptool.cmds.write_flash(esp, sorted(images), flash_mode='keep', flash_freq='keep', flash_size='keep')
    for offset, data in images:
        if esp.flash_md5sum(offset, len(data)).lower() != hashlib.md5(data).hexdigest():
            raise RuntimeError('Device readback mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--seed-build', type=Path, required=True)
    parser.add_argument('--rescue-build', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.read_bytes()
    if len(backup) != 0x1000000 or hashlib.sha256(backup).hexdigest() != H['BACKUP_SHA256']:
        raise RuntimeError('Verified original complete backup required')
    seed = (args.seed_build / 'wifi_rescue_seed.bin').read_bytes()
    rescue = (args.rescue_build / 'zone_wifi_rescue.bin').read_bytes()
    for build in (args.seed_build, args.rescue_build):
        sdk = (build / 'sdkconfig').read_text()
        if 'CONFIG_SECURE_BOOT=y' in sdk or 'CONFIG_NVS_ENCRYPTION=y' in sdk:
            raise RuntimeError('This experiment accepts unsigned unencrypted spare fixtures only')
        A['validate_partitions'](build / 'partition_table/partition-table.bin')
    if b'SLIC_WIFI_RESCUE_UNSIGNED_TEST_ONLY' not in rescue:
        raise RuntimeError('Not the unsigned spare-only test application')
    args.output.mkdir(parents=True, exist_ok=False)
    entry = struct.pack('<I20sII', 2, b'\xff'*20, 2, zlib.crc32(struct.pack('<I', 2)) ^ 0xffffffff)
    ota = entry + b'\xff'*(4096-len(entry))
    fixture = bytearray(b'\xff' * H['STORAGE_SIZE'])
    sentinel = hashlib.sha256(b'ADD Wi-Fi rescue attendance fixture v1').digest()*128
    fixture[:4096], fixture[-4096:] = sentinel, sentinel[::-1]
    expected = [hashlib.md5(fixture[i:i+0x100000]).hexdigest()
                for i in range(0, len(fixture), 0x100000)]
    result = {'mac': H['MAC'], 'purpose': 'unsigned_spare_wifi_rescue_experiment',
              'efuse_burns_performed': False, 'signed_lahore_recovery_qualified': False,
              'factory_acceptance': 'NOT_ASSERTED',
              'seed_application_sha256': A['application_digest'](args.seed_build / 'wifi_rescue_seed.bin', 'wifi_rescue_seed', '0.0.1'),
              'rescue_file_sha256': hashlib.sha256(rescue).hexdigest()}
    esp, before_security = H['connect'](args.port)
    # From this point a finally block restores and verifies every original byte.
    try:
        images = [(0, (args.seed_build / 'bootloader/bootloader.bin').read_bytes()),
                  (0x10000, (args.seed_build / 'partition_table/partition-table.bin').read_bytes()),
                  (0x11000, b'\xff'*0x6000), (0x17000, ota*2), (0x520000, seed),
                  (H['STORAGE_START'], bytes(fixture))]
        write_verified(esp, images)
        H['boot_application'](esp)
        esp._port.close()
        capture(args.port, args.output / 'seed-before.bin',
                b'ssid_state=EMPTY password_matches_target=0 other_fixture_intact=1')
        esp, security = H['connect'](args.port)
        before_storage = H['storage_digests'](esp)
        if before_storage != expected or security != before_security:
            raise RuntimeError('Initial preservation fixture mismatch')
        predecessor_md5 = esp.flash_md5sum(0x520000, len(seed)).lower()
        write_verified(esp, [(0x20000, rescue), (0x2a0000, rescue)])
        # Exercise the same two separately written OTA sectors as the future installer.
        write_verified(esp, [(0x17000, b'\xff'*4096)])
        write_verified(esp, [(0x18000, b'\xff'*4096)])
        H['boot_application'](esp)
        esp._port.close()
        transcript = capture(args.port, args.output / 'repair-and-original.bin',
                             b'ssid_state=RE7 password_matches_target=1 other_fixture_intact=1')
        if b'status=SUCCESS changed=1 other_keys_unchanged=1 storage_unchanged=1' not in transcript:
            raise RuntimeError('Rescue preservation evidence missing')
        esp, after_security = H['connect'](args.port)
        result.update(storage_unchanged=H['storage_digests'](esp) == before_storage == expected,
                      original_application_unchanged=esp.flash_md5sum(0x520000, len(seed)).lower() == predecessor_md5,
                      security_unchanged=after_security == before_security,
                      other_settings_unchanged=True, wifi_repaired=True, returned_original=True)
        if not all(result[k] for k in ['storage_unchanged', 'original_application_unchanged', 'security_unchanged']):
            raise RuntimeError('Preservation verification failed')
    finally:
        if esp._port.is_open:
            esp._port.close()
        esp, _ = H['connect'](args.port)
        write_verified(esp, [(0, backup)])
        for offset in range(0, len(backup), 0x100000):
            if esp.flash_md5sum(offset, 0x100000).lower() != hashlib.md5(backup[offset:offset+0x100000]).hexdigest():
                raise RuntimeError('Full original backup restoration verification failed')
        if esp.get_security_info(cache=False) != before_security:
            raise RuntimeError('Security changed')
        result['complete_original_flash_restored_and_verified'] = True
        H['boot_application'](esp)
        esp._port.close()
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
