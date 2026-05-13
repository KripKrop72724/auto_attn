from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SERVICE_NAME = "ZKZoneAgentService"
SERVICE_DISPLAY_NAME = "ZK Zone Agent Service"
SERVICE_DESCRIPTION = "Local ZKTeco attendance fraud-monitoring zone agent."


def run_console() -> None:
    from zk_zone_agent.__main__ import main

    main()


if sys.platform == "win32":
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore

    class ZKZoneAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.process: subprocess.Popen | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.process:
                self.process.terminate()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            exe = Path(sys.executable)
            self.process = subprocess.Popen(
                [str(exe), "-m", "zk_zone_agent", "--host", "127.0.0.1", "--port", "7860"],
                cwd=str(Path(sys.executable).parent),
            )
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
else:
    ZKZoneAgentService = None


def main() -> None:
    if sys.platform == "win32":
        import win32serviceutil  # type: ignore

        win32serviceutil.HandleCommandLine(ZKZoneAgentService)
    else:
        run_console()


if __name__ == "__main__":
    main()
