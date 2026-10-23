import hashlib
import json
import runpy
from pathlib import Path

import pytest

validate = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                             'tools/usb-wifi-rescue/build.py'))['validate_qualification']


def test_packager_requires_matching_physical_security_and_preservation_evidence(tmp_path):
    signed = b'synthetic unit fixture; not a signed or flashable image'
    digest = hashlib.sha256(signed).hexdigest()
    (tmp_path / 'zone-lite-signed.bin').write_bytes(signed)
    manifest = {'image_role':'usb-wifi-rescue', 'target_mac':'14:c1:9f:46:ae:1c',
                'application_sha256':digest, 'git_sha':'a'*40}
    (tmp_path / 'bootstrap-manifest.json').write_text(json.dumps(manifest))
    report = {'purpose':'signed_secure_download_wifi_rescue_qualification',
              'target_mac':'14:c1:9f:46:ae:1c', 'signed_image_sha256':digest,
              'source_git_sha':'a'*40, 'efuse_burns_during_repair':False,
              'security_flags_before':0xc5, 'security_flags_after':0xc5,
              'key_purposes':[8,9,10,11,0,0,12], 'flash_crypt_count':0,
              'evidence_sha256':{'serial.bin':'b'*64}}
    for name in ['encrypted_nvs_verified','storage_unchanged','other_settings_unchanged',
                 'original_application_unchanged','returned_original','wifi_repaired',
                 'retry_verified','interruption_recovery_verified']:
        report[name] = True
    assert validate(tmp_path, report) == signed
    unsigned = {'purpose':'unsigned_spare_wifi_rescue_experiment', 'wifi_repaired':True}
    with pytest.raises(ValueError, match='qualification is required'):
        validate(tmp_path, unsigned)
    for field, value in [('storage_unchanged',False), ('security_flags_after',0),
                         ('interruption_recovery_verified',False), ('source_git_sha','c'*40),
                         ('signed_image_sha256','d'*64), ('efuse_burns_during_repair',True)]:
        with pytest.raises(ValueError, match='qualification is required'):
            validate(tmp_path, {**report, field:value})
    (tmp_path / 'zone-lite-signed.bin').write_bytes(signed+b'changed')
    with pytest.raises(ValueError, match='manifest'):
        validate(tmp_path, report)
