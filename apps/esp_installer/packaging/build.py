"""Build the State Life Device Installer for Windows.

    python packaging/build.py              TEST build when the secrets are absent
    python packaging/build.py --release    must find both secrets (field release)

Steps: field defaults, icon and installer pictures, version resource,
third-party notices, the PyInstaller folder (dist/State Life Device Installer),
the esptool source that its GPL licence asks us to pass on, the Inno Setup
installer and SHA256SUMS.txt. Outside Windows the installer step is skipped;
that is how the dev container checks the PyInstaller recipe.

Release secrets, read from the environment and never printed:
    ESP_INSTALLER_FIELD_DEFAULTS_JSON  JSON object with the api.FieldDefaults keys
                                       (all except setup_ap_password and is_test_build)
    ZONE_LITE_SETUP_PASSWORD           the boards' setup hotspot password

Run it with the locked build environment (requirements.lock and
requirements-build.lock) so the bundle and the notices match the locks.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import string
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

PACKAGING = Path(__file__).resolve().parent
APP = PACKAGING.parent
SRC = APP / "src"
REPO = APP.parents[1]
BUILD = APP / "build"
DIST = APP / "dist"
BRANDING = BUILD / "branding"
APP_NAME = "State Life Device Installer"
APP_DIR = DIST / APP_NAME
SETUP_PREFIX = "StateLife-Device-Installer-Setup-"
LOGO = REPO / "apps" / "add_frontend" / "public" / "state-life-logo.png"
RESOURCES = SRC / "sl_installer" / "resources"
FIELD_DEFAULTS = RESOURCES / "field_defaults.json"
RUNTIME_LOCK = APP / "requirements.lock"
DEFAULTS_ENV = "ESP_INSTALLER_FIELD_DEFAULTS_JSON"
SETUP_PASSWORD_ENV = "ZONE_LITE_SETUP_PASSWORD"
SECRET_ENVS = (DEFAULTS_ENV, SETUP_PASSWORD_ENV)
BRAND_BLUE = "#0094DA"
WHITE = "#FFFFFF"

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# Inno Setup picks the picture that best fits the screen scale (100-250 %).
WIZARD_SIZES = {100: (164, 314), 125: (192, 386), 150: (246, 459), 175: (273, 556),
                200: (328, 604), 225: (355, 700), 250: (410, 797)}
WIZARD_SMALL_SIZES = {100: (55, 55), 125: (64, 68), 150: (83, 80), 175: (92, 97),
                      200: (110, 106), 225: (119, 123), 250: (138, 140)}

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(PACKAGING))


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def clean_environment() -> Dict[str, str]:
    """The environment for child processes: the secrets stay in this process."""
    return {key: value for key, value in os.environ.items() if key not in SECRET_ENVS}


# ------------------------------------------------------------------ pictures

def _square_logo():
    from PySide6.QtGui import QColor, QImage, QPainter

    logo = QImage(str(LOGO))
    if logo.isNull():
        raise SystemExit(f"Cannot read the logo at {LOGO}")
    side = max(logo.width(), logo.height())
    square = QImage(side, side, QImage.Format.Format_ARGB32)
    square.fill(QColor(WHITE))
    painter = QPainter(square)
    painter.drawImage((side - logo.width()) // 2, (side - logo.height()) // 2, logo)
    painter.end()
    return square


def _scaled(image, width: int, height: int):
    from PySide6.QtCore import Qt

    return image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


def _png_bytes(image) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _dib_bytes(image) -> bytes:
    """A 32-bit icon bitmap: BGRA rows bottom-up, then an empty AND mask."""
    size = image.width()
    pixels = bytes(image.constBits())[: image.sizeInBytes()]
    stride = image.bytesPerLine()
    rows = [pixels[y * stride:y * stride + size * 4] for y in range(size)]
    colour = b"".join(reversed(rows))
    mask = bytes(((size + 31) // 32) * 4 * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(colour) + len(mask), 0, 0, 0, 0)
    return header + colour + mask


def write_icon(square, path: Path) -> None:
    """A multi-size .ico: bitmaps up to 128 px (read by every Windows tool), PNG at 256."""
    from PySide6.QtGui import QImage

    images = []
    for size in ICON_SIZES:
        image = _scaled(square, size, size).convertToFormat(QImage.Format.Format_ARGB32)
        images.append((size, _png_bytes(image) if size >= 256 else _dib_bytes(image)))
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = [], []
    for size, blob in images:
        side = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    path.write_bytes(header + b"".join(entries) + b"".join(blobs))


def _wizard_picture(square, width: int, height: int, small: bool):
    from PySide6.QtGui import QColor, QImage, QPainter

    canvas = QImage(width, height, QImage.Format.Format_RGB32)
    canvas.fill(QColor(WHITE))
    painter = QPainter(canvas)
    if small:
        side = round(min(width, height) * 0.9)
        top = (height - side) // 2
    else:
        side = round(min(width * 0.72, height * 0.42))
        top = round(height * 0.36 - side / 2)
        band = max(2, round(height * 0.035))
        painter.fillRect(0, height - band, width, band, QColor(BRAND_BLUE))
    painter.drawImage((width - side) // 2, top, _scaled(square, side, side))
    painter.end()
    return canvas.convertToFormat(QImage.Format.Format_RGB888)


def make_branding() -> None:
    BRANDING.mkdir(parents=True, exist_ok=True)
    square = _square_logo()
    write_icon(square, BRANDING / "app.ico")
    for scale, (width, height) in WIZARD_SIZES.items():
        _wizard_picture(square, width, height, small=False).save(
            str(BRANDING / f"wizard-{scale}.bmp"), "BMP")
    for scale, (width, height) in WIZARD_SMALL_SIZES.items():
        _wizard_picture(square, width, height, small=True).save(
            str(BRANDING / f"wizard-small-{scale}.bmp"), "BMP")
    print(f"Icon and installer pictures written to {BRANDING}")


# ------------------------------------------------------------ field defaults

def _field_problems(values: Dict[str, object], setup_password: str) -> List[str]:
    """Names of fields whose values the app itself would not accept."""
    from sl_installer.core import validation
    from sl_installer.core.api import ZktSettings

    problems = []
    if validation.check_wifi(str(values["wifi_ssid"]), str(values["wifi_password"])):
        problems.append("wifi_ssid / wifi_password")
    if validation.check_device_id(str(values["device_id"])):
        problems.append("device_id")
    zkt = ZktSettings(port=values["zkt_port"], comm_key=values["zkt_comm_key"],
                      preferred_ip=values["zkt_preferred_ip"])
    if validation.check_zkt(zkt):
        problems.append("zkt_port / zkt_comm_key / zkt_preferred_ip")
    if not 1 <= int(values["hik_port"]) <= 65535:
        problems.append("hik_port")
    if values["hik_transport"] not in validation.HIK_TRANSPORTS:
        problems.append("hik_transport")
    username = str(values["hik_username"])
    if not username or len(username.encode("utf-8")) > validation.HIK_LIMITS["hik_username"]:
        problems.append("hik_username")
    # A Wi-Fi (WPA2) hotspot needs 8 to 63 characters.
    if not 8 <= len(setup_password.encode("utf-8")) <= 63:
        problems.append(SETUP_PASSWORD_ENV)
    return problems


def field_defaults(require_release: bool) -> Optional[Dict[str, object]]:
    """The release defaults to bake in, checked against the app's own rules, or
    None for a TEST build (no secrets: the app falls back to api.FieldDefaults)."""
    from sl_installer.core.api import FieldDefaults

    defaults = dataclasses.asdict(FieldDefaults())
    raw = os.environ.get(DEFAULTS_ENV, "").strip()
    setup_password = os.environ.get(SETUP_PASSWORD_ENV, "")
    if not raw and not setup_password:
        if require_release:
            raise SystemExit(f"A release needs {DEFAULTS_ENV} and {SETUP_PASSWORD_ENV}.")
        return None
    if not raw or not setup_password:
        raise SystemExit(f"Set both {DEFAULTS_ENV} and {SETUP_PASSWORD_ENV}, or neither.")
    try:
        values = json.loads(raw)
    except ValueError:
        raise SystemExit(f"{DEFAULTS_ENV} is not valid JSON.") from None
    if not isinstance(values, dict):
        raise SystemExit(f"{DEFAULTS_ENV} must be a JSON object.")
    expected = set(defaults) - {"setup_ap_password", "is_test_build"}
    unknown, missing = sorted(set(values) - expected), sorted(expected - set(values))
    if unknown or missing:
        raise SystemExit(f"{DEFAULTS_ENV}: unknown keys {unknown}, missing keys {missing}.")
    wrong_type = sorted(key for key in expected
                        if type(values[key]) is not type(defaults[key]))
    if wrong_type:
        raise SystemExit(f"{DEFAULTS_ENV}: wrong value type for {wrong_type}.")
    problems = _field_problems(values, setup_password)
    if problems:
        raise SystemExit(f"These defaults would be refused by the app: {', '.join(problems)}.")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # GitHub masks each whole secret, not the values inside the JSON one.
        for value in (values["wifi_password"], str(values["zkt_comm_key"]), setup_password):
            if len(value) >= 4:
                print(f"::add-mask::{value}")
    return dict(values, setup_ap_password=setup_password)


def write_field_defaults(require_release: bool) -> bool:
    """Write resources/field_defaults.json for a release; True if this is one.

    sl_installer.config treats the file's presence as "release build", so a
    TEST build must not have one, not even a stale one from an earlier build.
    """
    values = field_defaults(require_release)
    if values is None:
        FIELD_DEFAULTS.unlink(missing_ok=True)
        print("TEST build: no field defaults secrets, so the app shows a TEST BUILD banner.")
        return False
    FIELD_DEFAULTS.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
    print(f"Release build: {len(values) - 1} field defaults and the setup hotspot password "
          "came from the build secrets.")
    return True


# ------------------------------------------------------------- the app folder

def app_version() -> str:
    from sl_installer import __version__

    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        raise SystemExit(f"sl_installer.__version__ must look like 1.2.3, not {__version__!r}")
    return __version__


def write_version_info(version: str, release: bool) -> None:
    numbers = tuple(int(part) for part in version.split(".")) + (0,)
    template = string.Template((PACKAGING / "version_info.txt").read_text(encoding="utf-8"))
    (BUILD / "version-info.txt").write_text(template.substitute(
        version=version, version_tuple=repr(numbers), app_name=APP_NAME,
        build_kind="Release build" if release else "TEST BUILD"), encoding="utf-8")


def run_pyinstaller() -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
         str(PACKAGING / "sl_installer.spec")],
        check=True,
        env=dict(clean_environment(), SL_INSTALLER_BUILD_DIR=str(BUILD)),
    )


def _locked_hashes(name: str) -> List[str]:
    text = RUNTIME_LOCK.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}==\S+.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise SystemExit(f"{name} is not in {RUNTIME_LOCK.name}")
    return re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group(0))


def esptool_source(destination: Path) -> Path:
    """The exact esptool source release, checked against the lock's hashes."""
    from importlib import metadata

    version = metadata.version("esptool")
    name = f"esptool-{version}.tar.gz"
    cached = BUILD / "source" / name
    if not cached.is_file():
        cached.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://files.pythonhosted.org/packages/source/e/esptool/{name}"
        with urllib.request.urlopen(url, timeout=120) as response:
            cached.write_bytes(response.read())
    if hashlib.sha256(cached.read_bytes()).hexdigest() not in _locked_hashes("esptool"):
        cached.unlink()
        raise SystemExit(f"{name} does not match the hashes in {RUNTIME_LOCK.name}")
    destination.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy2(cached, destination / name))


def check_app_folder(release: bool) -> None:
    """Fail now, not on a field PC, when something the app loads is missing."""
    internal = APP_DIR / "_internal"
    bundled_defaults = internal / "sl_installer" / "resources" / FIELD_DEFAULTS.name
    if bundled_defaults.exists() != release:
        raise SystemExit(f"A {'release' if release else 'TEST'} build must "
                         f"{'' if release else 'not '}contain {FIELD_DEFAULTS.name}.")
    required = [internal / "sl_installer" / "resources" / path.relative_to(RESOURCES)
                for path in RESOURCES.rglob("*") if path.is_file()
                and "__pycache__" not in path.parts]
    required += [
        internal / "esptool" / "targets" / "stub_flasher" / "1" / "esp32s3.json",
        internal / "espefuse" / "efuse_defs" / "esp32s3.yaml",
        internal / "certifi" / "cacert.pem",
        APP_DIR / "EULA.txt",
        APP_DIR / "THIRD-PARTY-NOTICES.txt",
    ]
    if sys.platform == "win32":
        required += [APP_DIR / f"{APP_NAME}.exe",
                     internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll"]
    elif sys.platform.startswith("linux"):
        required += [APP_DIR / APP_NAME,
                     internal / "PySide6" / "Qt" / "plugins" / "platforms" / "libqoffscreen.so"]
    missing = [str(path.relative_to(APP_DIR)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("The app folder is missing:\n  " + "\n  ".join(missing))
    print(f"App folder checked: {sum(1 for p in APP_DIR.rglob('*') if p.is_file())} files.")


# ---------------------------------------------------------------- installer

def find_iscc() -> Path:
    configured = os.environ.get("ISCC")
    candidates = [Path(configured)] if configured else []
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(Path(program_files) / "Inno Setup 6" / "ISCC.exe")
    found = shutil.which("iscc")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("Inno Setup 6 (ISCC.exe) was not found; set ISCC to its path.")


def build_installer(version: str, release: bool) -> Path:
    test_flag = [] if release else ["/DTestBuild"]
    subprocess.run(
        [str(find_iscc()), f"/DAppVersion={version}", *test_flag, f"/DAppDir={APP_DIR}",
         f"/DBrandDir={BRANDING}", f"/DOutputDir={DIST}", str(PACKAGING / "installer.iss")],
        check=True,
        env=clean_environment(),
    )
    setup = DIST / f"{SETUP_PREFIX}{version}{'' if release else '-TEST'}.exe"
    if not setup.is_file():
        raise SystemExit(f"Inno Setup did not write {setup.name}")
    return setup


def write_checksums(files: List[Path]) -> Path:
    sums = DIST / "SHA256SUMS.txt"
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files]
    sums.write_text("".join(lines), encoding="utf-8")
    return sums


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", action="store_true",
                        help="fail unless the field defaults secrets are set")
    parser.add_argument("--no-installer", action="store_true",
                        help="stop after the app folder (automatic outside Windows)")
    options = parser.parse_args(argv)

    BUILD.mkdir(parents=True, exist_ok=True)
    for old in list(DIST.glob(f"{SETUP_PREFIX}*.exe")) + [DIST / "SHA256SUMS.txt"]:
        if old.is_file():
            old.unlink()
    version = app_version()
    try:
        step("Field defaults")
        release = write_field_defaults(options.release)
        step("Icon and installer pictures")
        make_branding()
        step("Version resource and third-party notices")
        write_version_info(version, release)
        import notices

        notices.write_notices(BUILD / "THIRD-PARTY-NOTICES.txt", version, RUNTIME_LOCK)
        step("PyInstaller")
        run_pyinstaller()
    finally:
        # The bundle has its copy; do not leave release values in the source tree.
        FIELD_DEFAULTS.unlink(missing_ok=True)
    step("Licences and source code")
    shutil.copy2(PACKAGING / "EULA.txt", APP_DIR / "EULA.txt")
    shutil.copy2(BUILD / "THIRD-PARTY-NOTICES.txt", APP_DIR / "THIRD-PARTY-NOTICES.txt")
    print(f"Passed on {esptool_source(APP_DIR / 'licenses').name}")
    check_app_folder(release)

    if options.no_installer or sys.platform != "win32":
        print(f"\nApp folder ready (no installer on {sys.platform}): {APP_DIR}")
        return 0
    step("Inno Setup")
    setup = build_installer(version, release)
    sums = write_checksums([setup])
    print(f"\n{'Release' if release else 'TEST'} installer: {setup}\nChecksums: {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
