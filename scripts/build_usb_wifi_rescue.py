"""Build quarantined rescue + synthetic predecessor; never signs or flashes."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from usb_wifi_rescue_artifacts import application_digest, validate_package

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def owned_build_directory():
    with tempfile.TemporaryDirectory(prefix='add-usb-rescue-build-') as temporary:
        scratch = Path(temporary)
        try:
            yield scratch
        finally:
            if sys.platform.startswith('linux'):
                # The IDF image builds as root. Restore ownership before the
                # non-root Linux runner deletes generated headers and objects.
                # Do not follow build symlinks outside this private scratch tree.
                subprocess.run(['docker', 'run', '--rm', '-v', f'{scratch}:/scratch',
                                '--entrypoint', '/bin/chown', 'espressif/idf:v5.5.3',
                                '-hR', f'{os.getuid()}:{os.getgid()}', '/scratch'], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Refusing to replace an existing output directory')
    password = os.environ.get('SLIC_RESCUE_WIFI_PASSWORD', '')
    if not 8 <= len(password.encode()) <= 63 or not password.isascii() or not password.isprintable():
        raise ValueError('An explicit printable ASCII recovery password is required')
    output.mkdir(parents=True)
    with owned_build_directory() as scratch:

        def build(project, name, options, env):
            command = ['docker', 'run', '--rm', '-e', 'SLIC_RESCUE_WIFI_PASSWORD',
                       '-e', 'SLIC_RESCUE_SPARE_PREDECESSOR_SHA256',
                       '-v', f'{ROOT}:/project:ro', '-v', f'{scratch}:/scratch',
                       '-w', '/project/' + project, 'espressif/idf:v5.5.3',
                       'idf.py', '-B', f'/scratch/{name}', '-D', f'SDKCONFIG=/scratch/{name}/sdkconfig',
                       *options, 'build']
            print('Building quarantined role:', name, flush=True)
            proc = subprocess.run(command, check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # Never export the configured password through compiler diagnostics.
            escaped = password.replace('\\', '\\\\').replace('"', '\\"')
            log = proc.stdout.decode(errors='replace').replace(escaped, '[REDACTED]').replace(password, '[REDACTED]')
            (output / f'{name}-build.log').write_text(log)
            if proc.returncode:
                print(log[-12000:], flush=True)
                raise RuntimeError(f'{name} build failed; redacted diagnostics retained')

        env = os.environ.copy()
        build('tests/firmware/hardware/wifi_rescue_seed', 'seed',
              ['-D', 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.secure.defaults'], env)
        seed_sha = application_digest(scratch / 'seed/wifi_rescue_seed.bin', 'wifi_rescue_seed', '0.0.1')
        env['SLIC_RESCUE_SPARE_PREDECESSOR_SHA256'] = seed_sha
        build('firmware/zone_lite/usb_wifi_rescue', 'rescue', [], env)
        reports = {}
        for name, project, role in [('seed', 'wifi_rescue_seed', 'usb-wifi-rescue-seed'),
                                    ('rescue', 'zone_wifi_rescue', 'usb-wifi-rescue')]:
            source = scratch / name
            dest = output / name
            dest.mkdir()
            for src, target in [('bootloader/bootloader.bin', 'bootloader.bin'),
                                ('partition_table/partition-table.bin', 'partition-table.bin'),
                                ('ota_data_initial.bin', 'ota_data_initial.bin'),
                                ('flasher_args.json', 'flasher_args.json'),
                                ('sdkconfig', 'sdkconfig'), (project + '.bin', 'zone_lite.bin')]:
                shutil.copy2(source / src, dest / target)
            reports[name] = validate_package(dest, role)
        (output / 'build-report.json').write_text(json.dumps(reports, indent=2) + '\n')
    print('Quarantined roles built; temporary files removed; no signing, flash writes or eFuse burns performed.')


if __name__ == '__main__':
    main()
