from __future__ import annotations

import os
import platform
import plistlib
import sys
from pathlib import Path


def set_login_start(enabled: bool) -> None:
    system = platform.system()
    executable = str(Path(sys.executable).resolve())
    if system == "Windows":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    "StateLifeADDProvisioningCompanion",
                    0,
                    winreg.REG_SZ,
                    f'"{executable}" --background',
                )
            else:
                try:
                    winreg.DeleteValue(key, "StateLifeADDProvisioningCompanion")
                except FileNotFoundError:
                    pass
        return
    if system == "Darwin":
        launch_agents = Path.home() / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        path = launch_agents / "com.statelife.add-provisioning-companion.plist"
        if not enabled:
            path.unlink(missing_ok=True)
            return
        payload = {
            "Label": "com.statelife.add-provisioning-companion",
            "ProgramArguments": [executable, "--background"],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(payload))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return
    raise RuntimeError("Login start is supported only on Windows and macOS.")
