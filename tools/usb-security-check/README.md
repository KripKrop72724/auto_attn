# Offline USB security check v1.3

This is a diagnostic executable, **not a repair installer**. It includes
Espressif esptool 5.1.0, its GPL license and upstream README. The Windows x64
computer needs no Python, installer or network download. Windows Forms and
PowerShell are supplied by Windows 10/11. The executable is unsigned.

Connect only the ESP's native USB port, open the EXE, and click Start. The native
startup monitor appears before PowerShell loads. The tool identifies a single
VID 303A/PID 1001 COM device and runs only `get-security-info`, with no flasher
stub. It requests a ROM reboot but does not write flash, erase data, burn eFuses
or read private keys. **Unplug and reconnect USB when prompted** to return to
normal boot. It then captures 210 seconds of serial output and Windows network
diagnostics, including a Wi-Fi observation after the recovery timeout. Send the
Desktop ZIP to support. Raw firmware output may contain sensitive information.

The security report establishes whether Secure Download Mode permits the
intended recovery protocol; it does not qualify a firmware image or assert
attendance preservation. It fails visibly and retains partial logs on errors.

Build using `prepare_tools.py <external-tools.zip>`, followed by `build.py
<zig-0.14.1> <external-tools.zip>`. Upstream and executable SHA-256 values are
pinned in `tool-metadata.json`. Generated payload and assembly files are ignored.
The Windows workflow exercises both GUI startup and the bundled tool's version
command with `--ui-smoke-test`; it performs no USB access in that mode.
