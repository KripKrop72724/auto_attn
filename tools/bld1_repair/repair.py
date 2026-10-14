"""One-device USB recovery UI. No eFuse operations or fleet secrets are bundled."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

TARGET = "ac:27:6e:a3:0a:58"
ZONE = "LF-ZONE-BLD1-01"
MARKER = f"BLD1_REPAIR_VERIFIED r1 mac={TARGET} zone={ZONE} serial=G04600476 user=admin"
APP_OFFSET = 0x20000
APP_SIZE = 0x280000
OTA_OFFSET = 0x17000
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD = ROOT / "payload"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def clean(text):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\b", "")


def usb_mac(port):
    # Native Espressif USB Serial/JTAG descriptor contains the factory MAC.
    # Bridge adapter serial numbers are not physical ESP identities.
    if port.vid != 0x303A or port.pid != 0x1001:
        return ""
    value = (port.serial_number or "").lower().replace(":", "").replace("-", "")
    return ":".join(value[i:i + 2] for i in range(0, 12, 2)) if re.fullmatch(r"[0-9a-f]{12}", value) else ""


def validate_payload(folder=PAYLOAD):
    meta = json.loads((folder / "repair.json").read_text())
    if meta["target_mac"] != TARGET or meta["zone"] != ZONE or meta["version"] != "3.1.1-bld1fix1":
        raise RuntimeError("This repair package is not for this device.")
    required = {"zone-lite-signed.bin", "partition-table.bin", "ota_data_initial.bin", "key-1-public.pem"}
    if set(meta["files"]) != required:
        raise RuntimeError("Unexpected repair package contents.")
    for name, expected in meta["files"].items():
        if digest((folder / name).read_bytes()) != expected:
            raise RuntimeError("Repair package integrity check failed. Obtain a fresh EXE.")
    image = (folder / "zone-lite-signed.bin").read_bytes()
    if not 4096 < len(image) <= APP_SIZE or len(image) % 4096:
        raise RuntimeError("Unexpected signed firmware size.")
    if image[0] != 0xE9 or image[48:80].split(b"\0")[0] != b"3.1.1-bld1fix1" or image[80:112].split(b"\0")[0] != b"zone_lite_hikvision":
        raise RuntimeError("Unexpected firmware identity.")
    if (folder / "ota_data_initial.bin").read_bytes() != b"\xff" * 8192:
        raise RuntimeError("Unexpected factory boot selection.")
    return meta


def check_mac(text, port):
    matches = set(re.findall(r"MAC(?: address)?\s*:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})", text, re.I))
    if matches and {x.lower() for x in matches} != {TARGET}:
        raise RuntimeError("WRONG ESP: no flash changes were made. Connect only the BLD1 ESP ending 0A:58.")
    descriptor = usb_mac(port)
    if descriptor and descriptor != TARGET:
        raise RuntimeError("WRONG ESP USB identity: no flash changes were made.")
    if not matches and descriptor != TARGET:
        raise RuntimeError("Cannot verify the physical ESP MAC. Use its native USB data connector; do not proceed with an unidentified adapter.")


def check_security(text):
    if not re.search(r"Secure Boot:\s*Enabled", text, re.I):
        raise RuntimeError("Secure Boot is not confirmed. This tool will not provision or burn eFuses.")
    if not re.search(r"Flash Encryption:\s*Disabled", text, re.I):
        raise RuntimeError("Unexpected flash encryption state. No flash changes were made.")
    if not re.search(r"BLOCK_KEY0\s*-\s*HMAC_UP", text):
        raise RuntimeError("Expected HMAC key purpose is missing. No flash changes were made.")


def site_result(line):
    if "Poll OK;" in line:
        return "online"
    if "Poll retrying;" in line:
        if "authentication_rejected" in line:
            return "auth"
        if "binding" in line or "identity_mismatch" in line:
            return "binding"
        return "pending"
    return None


class Repair:
    def __init__(self, emit):
        self.emit = emit

    def tool(self, module, args, work, timeout=90, allow_failure=False):
        output = work / "tool-output.txt"
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve()))
        command += ["--tool", module, str(output), *map(str, args)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + timeout
        previous = ""
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    raise RuntimeError("USB operation timed out. Keep this ESP and run Repair again; if necessary follow the BOOT/RESET instructions.")
                if output.exists():
                    text = clean(output.read_text(errors="replace"))
                    percentages = re.findall(r"\((\d+(?:\.\d+)?)\s*%\)", text)
                    if percentages and percentages[-1] != previous:
                        previous = percentages[-1]
                        self.emit("progress", f"USB transfer: {previous}% — do not unplug")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        text = clean(output.read_text(errors="replace")) if output.exists() else ""
        output.unlink(missing_ok=True)
        if process.returncode and not allow_failure:
            # Tool output can include arbitrary serial bytes. Keep the UI/log allowlisted.
            raise RuntimeError("USB operation failed. Check the USB data cable and close other serial programs. If necessary: hold BOOT, tap RESET, release BOOT, then click Repair again. No eFuses are changed.")
        return process.returncode, text

    def run(self):
        from serial.tools import list_ports
        import serial
        self.emit("status", "Checking the bundled signed repair image…")
        meta = validate_payload()
        with tempfile.TemporaryDirectory(prefix="BLD1-repair-") as temp:
            work = Path(temp)
            self.tool("espsecure", ["verify_signature", "--version", "2", "--keyfile", PAYLOAD / "key-1-public.pem", PAYLOAD / "zone-lite-signed.bin"], work)
            ports = [p for p in list_ports.comports() if p.vid in {0x303A, 0x10C4, 0x1A86, 0x0403}]
            if len(ports) != 1:
                raise RuntimeError("Connect exactly ONE ESP using a USB data cable. If no port appears, use the native USB connector or have IT install the board manufacturer's USB driver. Then click Repair again.")
            port = ports[0]
            if usb_mac(port) and usb_mac(port) != TARGET:
                raise RuntimeError("Wrong ESP connected. This package is ONLY for AC:27:6E:A3:0A:58.")
            self.emit("status", "Identifying ESP and checking existing security…")
            base = ["--chip", "esp32s3", "--port", port.device, "--baud", "115200", "--no-stub", "--before", "default-reset", "--after", "no-reset"]
            _, security = self.tool("esptool", base + ["get-security-info"], work)
            check_security(security)
            _, identity = self.tool("esptool", base + ["read-mac"], work, allow_failure=True)
            check_mac(identity, port)
            # A secured ROM may deny reads. Only the exact native factory USB MAC
            # can qualify that path; no generic adapter/typed MAC override exists.
            secure_rom = bool(re.search(r"Secure Download (?:Mode)?\s*(?:is|:)?\s*Enabled", security, re.I))
            if secure_rom and usb_mac(port) != TARGET:
                raise RuntimeError("Secure ROM requires the exact native USB identity. No writes performed.")
            if not secure_rom:
                self.tool("esptool", base + ["read-flash", "0x10000", "0x1000", work / "partitions.bin"], work)
                table = (work / "partitions.bin").read_bytes()
                expected = (PAYLOAD / "partition-table.bin").read_bytes()
                if table[:len(expected)] != expected or any(b != 255 for b in table[len(expected):]):
                    raise RuntimeError("Unexpected flash layout. No writes performed.")
            self.emit("status", "Correct ESP verified. Writing signed application only — keep USB connected.")
            self.emit("log", "Target and security verified. Wi-Fi/NVS, keys, bootloader and attendance storage are not erased.")
            self.tool("esptool", base + ["write-flash", "--no-compress", "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep", hex(APP_OFFSET), PAYLOAD / "zone-lite-signed.bin"], work, timeout=480)
            if not secure_rom:
                self.tool("esptool", base + ["verify-flash", hex(APP_OFFSET), PAYLOAD / "zone-lite-signed.bin"], work, timeout=180)
                self.emit("log", "Signed application flash digest verified.")
            else:
                self.emit("log", "Secure ROM restricts readback; boot/signature and repair evidence must confirm completion.")
            # Activate factory only after the application write succeeds. Do not
            # touch the bootloader, partition table, NVS or 8MB attendance store.
            self.tool("esptool", base + ["write-flash", "--no-compress", "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep", hex(OTA_OFFSET), PAYLOAD / "ota_data_initial.bin"], work)
            if not secure_rom:
                self.tool("esptool", base + ["verify-flash", hex(OTA_OFFSET), PAYLOAD / "ota_data_initial.bin"], work)
            self.emit("status", "Firmware written. Restarting and checking the correction…")
            # No output-only shortcut marks success. The running signed app must
            # report exact identity and the committed/read-back NVS username.
            reset = base.copy()
            reset[reset.index("no-reset")] = "hard-reset"
            self.tool("esptool", reset + ["get-security-info"], work, allow_failure=True)
            deadline = time.monotonic() + 180
            verified = False
            result = None
            started = time.monotonic()
            prompted = False
            while time.monotonic() < deadline:
                current = [p for p in list_ports.comports() if usb_mac(p) == TARGET or (p.device == port.device and p.vid == port.vid and p.serial_number == port.serial_number)]
                if not current:
                    time.sleep(1)
                    continue
                try:
                    connection = serial.Serial()
                    connection.port = current[0].device
                    connection.baudrate = 115200
                    connection.timeout = 1
                    connection.dtr = False
                    connection.rts = False
                    connection.open()
                    with connection:
                        while time.monotonic() < deadline:
                            line = connection.readline(4096).decode("utf-8", "replace")
                            if "BLD1_REPAIR_REFUSED" in line:
                                raise RuntimeError("Firmware refused mismatched provisioning. Nothing in NVS was changed. Send this result to IT.")
                            if MARKER in line:
                                verified = True
                                self.emit("log", "Username correction committed and read back on the exact BLD1 ESP.")
                                self.emit("status", "Username repair verified. Checking the Hikvision terminal…")
                            event = site_result(line)
                            if event:
                                result = event
                            if verified and result == "online":
                                self.emit("done", "REPAIR VERIFIED — Hikvision polling succeeded. Leave the ESP powered and ask IT to confirm attendance delivery in ADD.")
                                return
                            if not verified and not prompted and time.monotonic() - started > 12:
                                self.emit("status", "Press RESET once (do not hold BOOT). Keep USB connected while the tool verifies the repair.")
                                prompted = True
                except serial.SerialException:
                    time.sleep(1)
            if verified:
                detail = {"auth": "The terminal still rejects the saved password/login; IT must verify the credential.", "binding": "The terminal identity differs from the saved binding; IT must verify its API serial.", "pending": "Terminal polling has not succeeded yet; IT must check the site/network."}.get(result, "No successful terminal poll was observed; check ADD with IT.")
                self.emit("pending", "USERNAME REPAIR VERIFIED — SITE CHECK PENDING. " + detail)
            else:
                raise RuntimeError("Firmware transfer completed, but running repair evidence was NOT observed. Do not treat this as complete. Send this result to IT; repeating Repair on the same ESP is safe.")


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title("BLD1 ESP Repair — State Life")
    root.geometry("760x570")
    root.minsize(700, 540)
    messages = queue.Queue()
    busy = False
    status = tk.StringVar(value="Connect only the BLD1 ESP using a USB data cable, then click Repair.")
    ttk.Label(root, text="LF-ZONE-BLD1-01 • Hikvision login repair", font=("Segoe UI", 17, "bold")).pack(pady=(20, 8))
    ttk.Label(root, text="Only ESP AC:27:6E:A3:0A:58 • HIK 3.1.1-bld1fix1\nKeeps Wi-Fi, saved password, serial, security keys and attendance storage.", justify="center").pack()
    ttk.Label(root, textvariable=status, wraplength=700, font=("Segoe UI", 11)).pack(fill="x", padx=25, pady=20)
    progress = ttk.Progressbar(root, mode="indeterminate")
    progress.pack(fill="x", padx=25)
    log = tk.Text(root, height=11, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=25, pady=15)
    ttk.Label(root, text="If USB is not detected: hold BOOT, tap RESET, release BOOT, then retry.\nDo not unplug or close this program while writing. No administrator password is required.", wraplength=700, justify="center").pack(pady=5)

    def emit(kind, text):
        messages.put((kind, text))

    def start():
        nonlocal busy
        busy = True
        button.config(state="disabled")
        progress.start()
        def work():
            try:
                Repair(emit).run()
            except Exception as exc:
                emit("error", str(exc))
            finally:
                emit("finished", "")
        threading.Thread(target=work, daemon=True).start()

    button = ttk.Button(root, text="Repair this ESP", command=start)
    button.pack(pady=(8, 20))

    def drain():
        nonlocal busy
        while not messages.empty():
            kind, text = messages.get()
            if kind == "finished":
                busy = False
                progress.stop()
                button.config(state="normal", text="Run repair again")
                continue
            status.set(text)
            if kind != "progress":
                log.config(state="normal")
                log.insert("end", text + "\n\n")
                log.see("end")
                log.config(state="disabled")
        root.after(150, drain)

    def close():
        if busy:
            messagebox.showwarning("Repair running", "Keep this window open and the USB connected until the operation finishes.")
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(150, drain)
    if "--gui-smoke-test" in sys.argv:
        root.after(1200, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--tool":
        module, output, *arguments = sys.argv[2:]
        if module not in {"esptool", "espsecure"}:
            raise SystemExit(2)
        with open(output, "w", encoding="utf-8", buffering=1) as stream:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                import importlib
                importlib.import_module(module).main(arguments)
    elif "--self-test" in sys.argv:
        report = Path(sys.argv[sys.argv.index("--self-test") + 1])
        validate_payload()
        with tempfile.TemporaryDirectory() as temp:
            worker = Repair(lambda *_: None)
            worker.tool("espsecure", ["verify_signature", "--version", "2", "--keyfile", PAYLOAD / "key-1-public.pem", PAYLOAD / "zone-lite-signed.bin"], Path(temp))
            worker.tool("esptool", ["version"], Path(temp))
        import serial.tools.list_ports
        import tkinter
        report.write_text(json.dumps({"package": "verified", "signature": "verified", "esptool": "bundled", "gui": "imported", "hardware_tested": False}))
    else:
        main()
