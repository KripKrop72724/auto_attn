"""Write THIRD-PARTY-NOTICES.txt for the frozen app.

The list of components is the runtime lock (requirements.lock), so it matches
what PyInstaller bundles. Licence texts come from each installed package's
metadata; ``packaging/licenses`` holds the few texts a package does not ship
(pyserial's wheel has no licence file, and the Qt wheels carry none at all).
"""
from __future__ import annotations

import re
import sys
import sysconfig
import textwrap
from importlib import metadata
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

PACKAGING = Path(__file__).resolve().parent
LICENSES = PACKAGING / "licenses"
RULE = "=" * 79
THIN_RULE = "-" * 79
LICENCE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
# Package names in the lock whose licence texts we keep in packaging/licenses.
VENDORED = {
    "pyserial": ["pyserial-LICENSE.txt"],
    "pyside6-essentials": ["LGPL-3.0.txt", "GPL-3.0.txt"],
    "shiboken6": ["LGPL-3.0.txt", "GPL-3.0.txt"],
}
# The flasher stub esptool 5.1.0 loads into the board: stub version 1 is its default,
# and the app never sets ESPTOOL_STUB_VERSION. esptool ships only its binaries (with
# esptool/targets/stub_flasher/1/README.md naming this release), not its source.
STUB_RELEASE = "v1.8.0"
STUB_SOURCE = ("https://github.com/espressif/esptool-legacy-flasher-stub/releases/tag/"
               + STUB_RELEASE)


def locked_names(lock: Path) -> List[str]:
    names = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "-")):
            names.append(re.split(r"[=<>!~; ]", line, maxsplit=1)[0])
    return names


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _licence(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    # Some packages put the whole licence text in this field; its first line
    # is then usually the licence's name.
    short = (meta.get("License") or "").strip().splitlines()
    if short and len(short[0].strip()) <= 80:
        return short[0].strip()
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return ", ".join(classifiers) or "see the licence text below"


def _home_page(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    urls = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for key in ("source", "source code", "repository", "homepage", "home"):
        if key in urls:
            return urls[key]
    return meta.get("Home-page") or (next(iter(urls.values())) if urls else "")


def _licence_files(dist: metadata.Distribution) -> List[Tuple[str, str]]:
    found = []
    for item in dist.files or []:
        parts = item.parts
        if not parts[0].endswith(".dist-info"):
            continue
        in_licenses_folder = len(parts) > 2 and parts[1] == "licenses"
        beside_metadata = len(parts) == 2 and LICENCE_FILE.match(parts[1])
        if in_licenses_folder or beside_metadata:
            text = Path(dist.locate_file(item)).read_text(encoding="utf-8", errors="replace")
            found.append(("/".join(parts[1:]), text))
    for name in VENDORED.get(_canonical(dist.metadata["Name"]), []):
        found.append((name, (LICENSES / name).read_text(encoding="utf-8")))
    return found


def _python_licence() -> str:
    candidates = [Path(sys.base_prefix) / "LICENSE.txt",
                  Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError("the Python licence text (LICENSE.txt) was not found")


def _summary(app_version: str, versions: Dict[str, str]) -> str:
    qt, pyside, esptool, pyinstaller = (
        versions["qt"], versions["pyside6-essentials"], versions["esptool"], versions["pyinstaller"])
    python = ".".join(str(part) for part in sys.version_info[:3])
    qt_minor = ".".join(qt.split(".")[:2])
    text = f"""
    State Life Device Installer {app_version}: third-party notices

    The State Life Device Installer is made by State Life Insurance Corporation
    of Pakistan. It contains the open-source software listed in this file. Each
    part keeps its own licence, and the full licence texts follow this summary.
    Nothing in the app's own licence (EULA.txt) limits the rights these
    licences give you.

    Python {python}
      Licence: Python Software Foundation License (PSF-2.0). The licence text,
      with the notices for the libraries that the Windows build of Python
      contains (such as OpenSSL, libffi, zlib, bzip2, xz, expat, SQLite and
      mpdecimal), is in the "Python" section below.

    Qt {qt} and PySide6 {pyside} (Qt for Python)
      Licence: GNU Lesser General Public License version 3 (LGPL-3.0-only).
      The app uses Qt and PySide6 as unmodified, dynamically linked libraries:
      the files in the "_internal\\PySide6" and "_internal\\shiboken6" folders.
      You may replace them with your own builds of the same versions.
      Source code: https://download.qt.io/official_releases/qt/{qt_minor}/{qt}/
      and https://download.qt.io/official_releases/QtForPython/ (PySide6 {pyside}).
      Qt also contains code from other projects under their own licences; see
      https://doc.qt.io/qt-6/licenses-used-in-qt.html. The LGPL-3.0 and GPL-3.0
      texts are in the "PySide6_Essentials" section below.

    esptool {esptool} (esptool, espefuse and espsecure)
      Licence: GNU General Public License version 2 or later
      (GPL-2.0-or-later). The app uses esptool, unmodified, as a separate
      program: it starts its own executable with "--tool esptool" or
      "--tool espefuse". In that process a small part of the app hands the
      arguments over to esptool, which does the rest. esptool's source code
      is in "licenses\\esptool-{esptool}.tar.gz" in the app folder and at
      https://github.com/espressif/esptool/tree/v{esptool}.

    esptool flasher stub (esptool-legacy-flasher-stub {STUB_RELEASE})
      Licence: GNU General Public License version 2 or later
      (GPL-2.0-or-later). To write a board, esptool loads this small program
      into the board's memory. The app uses only this stub, the files in
      "_internal\\esptool\\targets\\stub_flasher\\1". Its source code is
      published at
      {STUB_SOURCE}
      and State Life head office can supply a copy on request. The stubs in
      "_internal\\esptool\\targets\\stub_flasher\\2", which the app does not
      use, are under the Apache-2.0 or MIT licence; their licence files are
      in that folder.

    PyInstaller {pyinstaller} (the start-up program inside the app's .exe)
      Licence: GPL-2.0-or-later with the PyInstaller bootloader exception,
      which allows it to be used in programs under other licences.

    The other components are listed below, each with its licence.
    """
    return textwrap.dedent(text).strip() + "\n"


def _section(title: str, licence: str, home: str, texts: Iterable[Tuple[str, str]]) -> str:
    lines = [RULE, title, f"Licence: {licence}"]
    if home:
        lines.append(f"Home page: {home}")
    for name, text in texts:
        lines += [THIN_RULE, f"[{name}]", "", text.strip(), ""]
    return "\n".join(lines) + "\n"


def write_notices(output: Path, app_version: str, runtime_lock: Path) -> None:
    from PySide6.QtCore import qVersion

    distributions = [metadata.distribution(name) for name in locked_names(runtime_lock)]
    distributions.append(metadata.distribution("pyinstaller"))
    versions = {_canonical(dist.metadata["Name"]): dist.version for dist in distributions}
    versions["qt"] = qVersion()

    parts = [_summary(app_version, versions), ""]
    python = ".".join(str(part) for part in sys.version_info[:3])
    parts.append(_section(f"Python {python}", "PSF-2.0", "https://www.python.org/",
                          [("LICENSE.txt", _python_licence())]))
    for dist in sorted(distributions, key=lambda d: _canonical(d.metadata["Name"])):
        texts = _licence_files(dist)
        if not texts:
            raise RuntimeError(f"no licence text found for {dist.metadata['Name']}")
        parts.append(_section(f"{dist.metadata['Name']} {dist.version}", _licence(dist),
                              _home_page(dist), texts))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Windows line ends, so every text viewer shows it properly.
    output.write_bytes("\n".join(parts).replace("\n", "\r\n").encode("utf-8"))
