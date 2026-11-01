"""Regenerate requirements.lock and requirements-build.lock, then prove they
install on the release target (Windows x64, CPython 3.12).

Run it inside the dev container, from apps/esp_installer:

    docker run --rm -v <repo>:/w -w /w/apps/esp_installer esp-installer-dev:local \
        sh -c "pip install -q pip-tools==7.6.2 && python packaging/update_locks.py"

pip-compile resolves for the machine it runs on, so packages that only
Windows needs are listed here explicitly and pinned without markers: a lock
made in the Linux container must still install on Windows under
``--require-hashes``. Each pin then gets the SHA-256 of every file PyPI has
for that version (Windows, Linux and macOS wheels and the sdist), read from
PyPI's JSON API instead of downloading hundreds of megabytes of Qt wheels.
The check at the end needs a Windows x64 / CPython 3.12 wheel for every pin
and walks the dependencies again with Windows markers.
"""
from __future__ import annotations

import email.parser
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List

from packaging import tags
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

APP = Path(__file__).resolve().parents[1]
WORK = APP / "build" / "locks"
RUNTIME_LOCK = APP / "requirements.lock"
BUILD_LOCK = APP / "requirements-build.lock"

# Needed on Windows only, so pip-compile on Linux would leave them out.
WINDOWS_RUNTIME = ["colorama"]  # rich-click (esptool's command line) on Windows
WINDOWS_BUILD = [
    "pefile>=2022.5.30,!=2024.8.26",  # PyInstaller on Windows
    "pywin32-ctypes>=0.2.1",  # PyInstaller on Windows
]
# PyPI has esptool only as source. The workflow installs this locked
# setuptools first and builds esptool with --no-build-isolation, so even the
# build backend is hash-checked.
BUILD_BACKEND = ["setuptools>=64"]
# Pure-Python packages that PyPI has only as source; every other pin needs a
# Windows wheel.
SOURCE_ONLY = {"esptool"}

WINDOWS_ENVIRONMENT = dict(
    default_environment(),
    implementation_name="cpython",
    os_name="nt",
    platform_machine="AMD64",
    platform_python_implementation="CPython",
    platform_system="Windows",
    python_full_version="3.12.10",
    python_version="3.12",
    sys_platform="win32",
    extra="",
)
WINDOWS_TAGS = set(tags.cpython_tags((3, 12), ["cp312"], ["win_amd64"])) | set(
    tags.compatible_tags((3, 12), "cp312", ["win_amd64"])
)


def compile_lock(source: Path, output: Path) -> None:
    subprocess.run(
        [
            sys.executable, "-m", "piptools", "compile", "--quiet",
            "--allow-unsafe", "--strip-extras", "--no-emit-index-url",
            "--no-emit-trusted-host", "--newline=LF",
            "--output-file", str(output.relative_to(APP)), str(source.relative_to(APP)),
        ],
        cwd=APP,
        env=dict(os.environ, CUSTOM_COMPILE_COMMAND="python packaging/update_locks.py"),
        check=True,
    )


def write_inputs(pyproject: Dict[str, object]) -> None:
    project = pyproject["project"]
    extras = project["optional-dependencies"]
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "runtime.in").write_text(
        "# Written by packaging/update_locks.py from pyproject.toml.\n"
        + "\n".join(list(project["dependencies"]) + WINDOWS_RUNTIME) + "\n",
        encoding="utf-8",
    )
    (WORK / "build.in").write_text(
        "# Written by packaging/update_locks.py from pyproject.toml.\n"
        "-c runtime-pins.txt\n"
        + "\n".join(list(extras["build"]) + list(extras["dev"]) + WINDOWS_BUILD + BUILD_BACKEND)
        + "\n",
        encoding="utf-8",
    )


def requirement_lines(lock: Path) -> Iterable[str]:
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "-")):
            yield line.rstrip(" \\")


def pin_of(line: str) -> "tuple[str, str]":
    requirement = Requirement(line)
    (version,) = [spec.version for spec in requirement.specifier]
    return canonicalize_name(requirement.name), version


def pypi_release(name: str, version: str) -> Dict[str, object]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def add_hashes(lock: Path, releases: Dict[str, Dict[str, object]]) -> None:
    lines = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith(("#", "-")):
            lines.append(line)
            continue
        name, version = pin_of(line)
        if name not in releases:
            releases[name] = pypi_release(name, version)
        digests = sorted({item["digests"]["sha256"] for item in releases[name]["urls"]})
        lines.append(line + " \\")
        lines.extend(f"    --hash=sha256:{digest} \\" for digest in digests[:-1])
        lines.append(f"    --hash=sha256:{digests[-1]}")
    lock.write_text("\n".join(lines) + "\n", encoding="utf-8")


def windows_requirements(release: Dict[str, object]) -> "List[str] | None":
    if canonicalize_name(release["info"]["name"]) in SOURCE_ONLY:
        if any(item["packagetype"] == "sdist" for item in release["urls"]):
            return list(release["info"].get("requires_dist") or [])
        return None
    for item in release["urls"]:
        if item["packagetype"] != "bdist_wheel":
            continue
        if not parse_wheel_filename(item["filename"])[3] & WINDOWS_TAGS:
            continue
        try:  # PEP 658: the index serves each wheel's METADATA beside it.
            with urllib.request.urlopen(item["url"] + ".metadata", timeout=60) as response:
                metadata = email.parser.Parser().parsestr(response.read().decode("utf-8"))
            return metadata.get_all("Requires-Dist") or []
        except urllib.error.HTTPError:
            return list(release["info"].get("requires_dist") or [])
    return None


def check_windows(locks: Iterable[Path], releases: Dict[str, Dict[str, object]]) -> List[str]:
    pins = dict(pin_of(line) for lock in locks for line in requirement_lines(lock))
    problems = []
    for owner in sorted(pins):
        requirements = windows_requirements(releases[owner])
        if requirements is None:
            problems.append(f"{owner}=={pins[owner]} has no Windows x64 / CPython 3.12 wheel"
                            " (or, if listed in SOURCE_ONLY, no source release)")
            continue
        for line in requirements:
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate(WINDOWS_ENVIRONMENT):
                continue
            name = canonicalize_name(requirement.name)
            if name not in pins:
                problems.append(f"{owner} needs {requirement} on Windows, but it is not locked")
            elif not requirement.specifier.contains(pins[name], prereleases=True):
                problems.append(f"{owner} needs {requirement}, but {name}=={pins[name]} is locked")
    return problems


def main() -> int:
    pyproject = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))
    write_inputs(pyproject)
    releases: Dict[str, Dict[str, object]] = {}
    compile_lock(WORK / "runtime.in", RUNTIME_LOCK)
    (WORK / "runtime-pins.txt").write_text(
        "\n".join(requirement_lines(RUNTIME_LOCK)) + "\n", encoding="utf-8"
    )
    add_hashes(RUNTIME_LOCK, releases)
    compile_lock(WORK / "build.in", BUILD_LOCK)
    add_hashes(BUILD_LOCK, releases)
    problems = check_windows([RUNTIME_LOCK, BUILD_LOCK], releases)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    print("Locks written; every pin has a Windows x64 / CPython 3.12 wheel and its dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
