# PyInstaller recipe for the State Life Device Installer: one folder, no console.
#
# Run it through packaging/build.py, which first writes the icon, the version
# resource and sl_installer/resources/field_defaults.json that this file uses.
# The same executable also runs the bundled esptool and espefuse when it is
# started as "<exe> --tool esptool|espefuse ..." (see sl_installer/__main__.py).
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_NAME = "State Life Device Installer"
PACKAGING = Path(SPECPATH)  # noqa: F821 - defined by PyInstaller
APP_DIR = PACKAGING.parent
SRC = APP_DIR / "src"
BUILD = Path(os.environ.get("SL_INSTALLER_BUILD_DIR", str(APP_DIR / "build")))

# collect_submodules imports the package, so it must be importable here.
sys.path.insert(0, str(SRC))

# Qt plugins the app needs; the PySide6 hooks collect many more. PNG support
# is part of Qt itself, so it needs no plugin. None keeps the whole group.
if sys.platform == "win32":
    PLATFORM_PLUGINS = {"qwindows"}
elif sys.platform == "darwin":
    PLATFORM_PLUGINS = {"qcocoa", "qoffscreen"}
else:
    # Linux builds only exist to test this recipe in the dev container.
    PLATFORM_PLUGINS = {"qoffscreen"}
QT_PLUGINS = {
    "platforms": PLATFORM_PLUGINS,
    "styles": None,
    "imageformats": {"qsvg", "qico"},
    "iconengines": {"qsvgicon"},
}
# Software OpenGL fallback: a widgets-only app never creates a GL context.
DROPPED_FILES = {"opengl32sw.dll"}

QT_MODULES_NOT_USED = [
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
    "QtBluetooth", "QtCharts", "QtConcurrent", "QtDataVisualization", "QtDBus", "QtDesigner",
    "QtExampleIcons", "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtHttpServer", "QtLocation",
    "QtMultimedia", "QtMultimediaWidgets", "QtNetwork", "QtNetworkAuth", "QtNfc", "QtOpenGL",
    "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning", "QtPrintSupport", "QtQml",
    "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickTest", "QtQuickWidgets",
    "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort", "QtSpatialAudio",
    "QtSql", "QtStateMachine", "QtTest", "QtTextToSpeech", "QtUiTools", "QtWebChannel",
    "QtWebEngineCore", "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets", "QtWebView",
    "QtXml",
]
EXCLUDES = [f"PySide6.{name}" for name in QT_MODULES_NOT_USED] + [
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3",
    "pytest", "_pytest", "pytestqt", "pip",
]


def wanted(dest_name):
    """False for Qt plugins, translations and files the app never loads."""
    parts = Path(dest_name).parts
    if parts[-1].lower() in DROPPED_FILES:
        return False
    if parts[0] != "PySide6":
        return True
    if "translations" in parts:
        return False  # the app is English only
    if "plugins" not in parts:
        return True
    group_index = parts.index("plugins") + 1
    if group_index >= len(parts) - 1:
        return True
    allowed = QT_PLUGINS.get(parts[group_index], set())
    if allowed is None:
        return True
    plugin = parts[-1].split(".")[0]
    if plugin.startswith("lib"):
        plugin = plugin[3:]
    return plugin in allowed


a = Analysis(  # noqa: F821 - defined by PyInstaller
    [str(PACKAGING / "launcher.py")],
    pathex=[str(SRC)],
    datas=[(str(SRC / "sl_installer" / "resources"), "sl_installer/resources")]
    # esptool loads its stub flasher JSON files, and espefuse its chip YAML
    # files, from beside their modules at run time.
    + collect_data_files("esptool")
    + collect_data_files("espefuse"),
    hiddenimports=collect_submodules("sl_installer")
    + collect_submodules("esptool")
    + collect_submodules("espefuse"),
    excludes=EXCLUDES,
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if wanted(entry[0])]
a.datas = [entry for entry in a.datas if wanted(entry[0])]

pyz = PYZ(a.pure)  # noqa: F821 - defined by PyInstaller

exe = EXE(  # noqa: F821 - defined by PyInstaller
    pyz,
    a.scripts,
    # UTF-8 mode: the same text encoding as the Linux and macOS test runs,
    # instead of the Windows ANSI code page.
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    disable_windowed_traceback=False,
    icon=str(BUILD / "branding" / "app.ico"),
    version=str(BUILD / "version-info.txt"),
    upx=False,
)

COLLECT(  # noqa: F821 - defined by PyInstaller
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
