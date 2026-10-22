"""Build a portable Windows x64 probe with the verified offline esptool archive."""
import hashlib
import json
import pathlib
import subprocess
import sys
import zipfile

root = pathlib.Path(__file__).resolve().parent
zig, archive = sys.argv[1], pathlib.Path(sys.argv[2]).resolve()
metadata = json.loads((root / 'tool-metadata.json').read_text())
with zipfile.ZipFile(archive) as package:
    if set(package.namelist()) != {'esptool.exe', 'LICENSE', 'README.md'}:
        raise SystemExit('Unexpected archive contents')
    if hashlib.sha256(package.read('esptool.exe')).hexdigest() != metadata['tool_sha256']:
        raise SystemExit('Unexpected esptool executable')
payload = (root / 'diagnose.ps1').read_bytes().replace(metadata['embedded_archive_sha256'].encode(), hashlib.sha256(archive.read_bytes()).hexdigest().encode())
(root / 'payload.h').write_text('static const unsigned char payload[] = {' + ','.join(map(str, payload)) + '};\n')
(root / 'tools.S').write_text('.section .rdata,"dr"\n.balign 16\n.globl tools_payload\n.globl tools_payload_end\ntools_payload:\n.incbin "' + archive.as_posix() + '"\ntools_payload_end:\n')
out = root.parents[1] / 'dist'
out.mkdir(exist_ok=True)
zig_env = json.loads(subprocess.check_output([zig, 'env'], text=True))
headers = pathlib.Path(zig_env['lib_dir']) / 'libc' / 'include' / 'any-windows-any'
subprocess.run([zig, 'cc', '-target', 'x86_64-windows-gnu', '-O2', '-nostdlib', '-fno-builtin', '-fno-stack-protector', '-isystem', str(headers), '-Wl,--subsystem,windows', '-Wl,--entry,WinMainCRTStartup', str(root / 'launcher.c'), str(root / 'tools.S'), '-lkernel32', '-luser32', '-lshell32', '-o', str(out / 'SLIC-USB-Security-Check-v1.3.exe')], check=True)
