# Windows Zone Agent Packaging

Run from an elevated PowerShell prompt on Windows:

```powershell
.\installer\windows\build.ps1
```

Then compile `installer\windows\ZKZoneAgent.iss` with Inno Setup.

The installer:

- Installs binaries under `C:\Program Files\ZK Zone Agent`
- Creates `C:\ProgramData\ZKZoneAgent`
- Installs `ZKZoneAgentService` with automatic startup
- Configures restart-on-failure recovery using `sc.exe failure`
- Starts the service and opens `http://127.0.0.1:7860/setup`

Local SQLite data is intentionally left behind on uninstall unless an operator removes it manually.
