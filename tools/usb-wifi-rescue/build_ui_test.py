"""Build a conspicuously disabled Windows GUI test, without repair firmware.

This cannot emit a customer repair installer. Production packaging remains
blocked on signed/encrypted-NVS/Secure Download hardware evidence.
"""
import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zig')
    parser.add_argument('tool_archive', type=Path)
    args = parser.parse_args()
    archive = args.tool_archive.resolve()
    metadata = json.loads((ROOT / 'tools/usb-security-check/tool-metadata.json').read_text())
    with zipfile.ZipFile(archive) as package:
        if set(package.namelist()) != {'esptool.exe', 'LICENSE', 'README.md'}:
            raise ValueError('Unexpected tool archive')
        if hashlib.sha256(package.read('esptool.exe')).hexdigest() != metadata['tool_sha256']:
            raise ValueError('Unexpected tool digest')
    payload = (Path(__file__).parent / 'repair.ps1').read_text()
    payload = payload.replace('__TARGET_MAC__', '14:c1:9f:46:ae:1c')
    payload = payload.replace('__ARCHIVE_SHA256__', hashlib.sha256(archive.read_bytes()).hexdigest())
    payload = payload.replace('__IMAGE_SHA256__', '0'*64)
    payload = payload.replace('[void]$form.ShowDialog()',
                              "$button.Enabled = $false\n$label.Text = 'UI TEST ONLY - repair is disabled; no firmware is bundled.'\n[void]$form.ShowDialog()")
    launcher = (ROOT / 'tools/usb-security-check/launcher.c').read_text()
    launcher = launcher.replace('SLIC USB Diagnostics v1.3', 'SLIC Wi-Fi Repair UI TEST ONLY')
    launcher = launcher.replace('diagnostics', 'repair').replace('Diagnostics', 'Repair')
    launcher = launcher.replace('diagnostic', 'repair').replace('Diagnostic', 'Repair')
    output = ROOT / 'dist/SLIC-WiFi-Repair-UI-TEST-ONLY.exe'
    output.parent.mkdir(exist_ok=True)
    zig_env = json.loads(subprocess.check_output([args.zig, 'env'], text=True))
    headers = Path(zig_env['lib_dir']) / 'libc/include/any-windows-any'
    with tempfile.TemporaryDirectory(prefix='slic-repair-ui-') as directory:
        work = Path(directory)
        (work / 'payload.h').write_text('static const unsigned char payload[] = {' + ','.join(map(str,payload.encode())) + '};\n')
        (work / 'launcher.c').write_text(launcher)
        (work / 'tools.S').write_text('.section .rdata,"dr"\n.balign 16\n.globl tools_payload\n.globl tools_payload_end\ntools_payload:\n.incbin "' + archive.as_posix() + '"\ntools_payload_end:\n')
        subprocess.run([args.zig, 'cc', '-target', 'x86_64-windows-gnu', '-O2', '-nostdlib',
                        '-fno-builtin', '-fno-stack-protector', '-isystem', str(headers),
                        '-Wl,--subsystem,windows', '-Wl,--entry,WinMainCRTStartup',
                        str(work / 'launcher.c'), str(work / 'tools.S'), '-lkernel32',
                        '-luser32', '-lshell32', '-o', str(output)], check=True)
    print('Disabled GUI test built:', output)


if __name__ == '__main__':
    main()
