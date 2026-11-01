# State Life Device Installer

A Windows 10/11 app (PySide6, with esptool 5.1.0 bundled) that site staff use
to prepare Zone Lite ESP32-S3 boards for ZKTeco or Hikvision attendance
machines, to find faulty boards, and to get plain instructions. It ships as a
PyInstaller folder inside an Inno Setup installer.

- Design: [docs/esp-installer-design.md](../../docs/esp-installer-design.md)
- Install, use, troubleshooting, release runbook and developer notes:
  [docs/esp-installer.md](../../docs/esp-installer.md)

Quick start (from the repository root):

```sh
# Tests, in the dev container (Python 3.12, Qt offscreen)
docker run --rm -v "$PWD":/w -w /w/apps/esp_installer -e PYTHONPATH=src \
  esp-installer-dev:local python -m pytest tests -q

# Python 3.9 syntax check on the Mac
python3 -m compileall -q apps/esp_installer/src

# From source: the app, or one of its other modes
PYTHONPATH=apps/esp_installer/src python -m sl_installer [--self-test | --screenshots DIR | --server URL]
```

Releases are built by `.github/workflows/esp-installer-release.yml` with
`packaging/build.py`; dependencies are hash-pinned in `requirements.lock` and
`requirements-build.lock` (regenerate with `packaging/update_locks.py`).
