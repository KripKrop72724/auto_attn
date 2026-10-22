"""Verify the official offline Windows tools and retain only the security CLI."""
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
destination = Path(sys.argv[1]).resolve()
source = destination.with_suffix('.upstream.zip')
url = ('https://github.com/espressif/esptool/releases/download/v5.1.0/'
       'esptool-v5.1.0-windows-amd64.zip')
metadata = json.loads((root / 'tool-metadata.json').read_text())
urllib.request.urlretrieve(url, source)
if hashlib.sha256(source.read_bytes()).hexdigest() != metadata['upstream_archive_sha256']:
    raise SystemExit('Upstream archive SHA-256 mismatch')
with (zipfile.ZipFile(source) as archive,
      zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, 9) as output):
    for name in ('esptool.exe', 'LICENSE', 'README.md'):
        item = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
        item.compress_type = zipfile.ZIP_DEFLATED
        output.writestr(item, archive.read('esptool-windows-amd64/' + name))
