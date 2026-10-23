"""Package only a physically qualified, signed one-shot rescue; never flash.

The qualification report must come from the actual locked-spare experiment.
This is separate from factory firmware promotion and never creates factory
acceptance receipts. The original signed bundle retains its HIL-only marker.
"""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPARE = '14:c1:9f:46:ae:1c'


def validate_qualification(bundle, report):
    manifest = json.loads((bundle / 'bootstrap-manifest.json').read_text())
    signed = (bundle / 'zone-lite-signed.bin').read_bytes()
    digest = hashlib.sha256(signed).hexdigest()
    required = ['encrypted_nvs_verified', 'storage_unchanged', 'other_settings_unchanged',
                'original_application_unchanged', 'returned_original', 'wifi_repaired',
                'retry_verified', 'interruption_recovery_verified']
    if (manifest.get('image_role') != 'usb-wifi-rescue' or manifest.get('target_mac') != SPARE
            or manifest.get('application_sha256') != digest
            or not re.fullmatch('[0-9a-f]{40}', manifest.get('git_sha', ''))):
        raise ValueError('Unexpected signed rescue manifest')
    if (report.get('purpose') != 'signed_secure_download_wifi_rescue_qualification'
            or report.get('target_mac') != SPARE or report.get('signed_image_sha256') != digest
            or report.get('source_git_sha') != manifest['git_sha']
            or any(report.get(k) is not True for k in required)
            or report.get('efuse_burns_during_repair') is not False
            or report.get('security_flags_before') != 0xc5
            or report.get('security_flags_after') != 0xc5
            or report.get('key_purposes') != [8,9,10,11,0,0,12]
            or report.get('flash_crypt_count') != 0):
        raise ValueError('Matching signed/encrypted-NVS/Secure Download qualification is required')
    hashes = report.get('evidence_sha256', {})
    if not hashes or any(not re.fullmatch('[0-9a-f]{64}', v) for v in hashes.values()):
        raise ValueError('Raw hardware evidence hashes are required')
    return signed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--qualification', type=Path, required=True)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--zig', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume-only', action='store_true', help='Capture the repair already installed by v1.1; no host flash operations')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Refusing to replace an existing executable')
    report = json.loads(args.qualification.read_text())
    signed = validate_qualification(args.bundle, report)
    metadata = json.loads((ROOT / 'tools/usb-security-check/tool-metadata.json').read_text())
    with zipfile.ZipFile(args.tools) as tools:
        if set(tools.namelist()) != {'esptool.exe', 'LICENSE', 'README.md'}:
            raise ValueError('Unexpected offline tool archive')
        if hashlib.sha256(tools.read('esptool.exe')).hexdigest() != metadata['tool_sha256']:
            raise ValueError('Offline tool digest differs')
        members = {name: tools.read(name) for name in tools.namelist()}
    # Verify the fleet RSA signature again before bundling it. Only public keys
    # are used locally; private fleet signing keys remain on the ADD runner.
    import sys
    subprocess.run([sys.executable, '-m', 'espsecure', 'verify-signature', '--version', '2',
                    '--keyfile', str(args.bundle / 'key-1-public.pem'),
                    str(args.bundle / 'zone-lite-signed.bin')], check=True)
    with tempfile.TemporaryDirectory(prefix='slic-qualified-repair-') as directory:
        work = Path(directory)
        archive = work / 'repair.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
            for name, data in members.items():
                package.writestr(name, data)
            package.writestr('rescue.bin', signed)
        payload = (Path(__file__).parent / 'repair.ps1').read_text()
        if args.resume_only:
            if payload.count('[switch]$ResumeOnly)') != 1:
                raise ValueError('Resume-only default cannot be safely embedded')
            payload = payload.replace('[switch]$ResumeOnly)', '[switch]$ResumeOnly = $true)')
        payload = payload.replace('__TARGET_MAC__', 'ac:27:6e:a3:16:d4')
        payload = payload.replace('__ARCHIVE_SHA256__', hashlib.sha256(archive.read_bytes()).hexdigest())
        payload = payload.replace('__IMAGE_SHA256__', hashlib.sha256(signed).hexdigest())
        (work / 'payload.h').write_text('static const unsigned char payload[] = {' + ','.join(map(str,payload.encode())) + '};\n')
        launcher = (ROOT / 'tools/usb-security-check/launcher.c').read_text()
        launcher = launcher.replace('SLIC USB Diagnostics v1.3', 'SLIC Attendance USB Wi-Fi Repair')
        launcher = launcher.replace('diagnostics','repair').replace('Diagnostics','Repair')
        launcher = launcher.replace('diagnostic','repair').replace('Diagnostic','Repair')
        (work / 'launcher.c').write_text(launcher)
        (work / 'tools.S').write_text('.section .rdata,"dr"\n.balign 16\n.globl tools_payload\n.globl tools_payload_end\ntools_payload:\n.incbin "' + archive.as_posix() + '"\ntools_payload_end:\n')
        zig_env = json.loads(subprocess.check_output([args.zig, 'env'], text=True))
        headers = Path(zig_env['lib_dir']) / 'libc/include/any-windows-any'
        subprocess.run([args.zig, 'cc', '-target', 'x86_64-windows-gnu', '-O2', '-nostdlib',
                        '-fno-builtin', '-fno-stack-protector', '-isystem', str(headers),
                        '-Wl,--subsystem,windows', '-Wl,--entry,WinMainCRTStartup',
                        str(work / 'launcher.c'), str(work / 'tools.S'), '-lkernel32',
                        '-luser32', '-lshell32', '-o', str(args.output)], check=True)
    print('Qualified repair executable built:', args.output)


if __name__ == '__main__':
    main()
