from __future__ import annotations

import asyncio
import platform
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from add_provisioning_companion import __version__
from add_provisioning_companion.client import CompanionClient, platform_id
from add_provisioning_companion.identity import load_or_create_identity
from add_provisioning_companion.startup import set_login_start

DASHBOARD = "https://attendancedevices.slichealth.com/firmware?tab=prepare"


class CompanionWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("State Life ADD Provisioning Companion")
        root.geometry("520x430")
        root.minsize(480, 400)
        root.configure(background="#f4f7fb")
        self.messages: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.status = tk.StringVar(value="Starting secure companion…")
        self.usb = tk.StringVar(value="Connect one ESP32-S3 with a data-capable USB cable.")
        self.code = tk.StringVar(value="— — — — — —")
        self.startup = tk.BooleanVar(value=False)

        frame = ttk.Frame(root, padding=28)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="STATE LIFE", foreground="#087bb9").pack(anchor="w")
        ttk.Label(
            frame,
            text="ADD Provisioning Companion",
            font=("Helvetica", 20, "bold"),
        ).pack(anchor="w", pady=(4, 4))
        ttk.Label(
            frame,
            text=f"Version {__version__} · {platform_id()}",
            foreground="#5b6b7c",
        ).pack(anchor="w")
        ttk.Separator(frame).pack(fill="x", pady=20)
        ttk.Label(frame, textvariable=self.status, font=("Helvetica", 12, "bold")).pack(
            anchor="w"
        )
        ttk.Label(frame, textvariable=self.usb, wraplength=450).pack(anchor="w", pady=(8, 18))
        pairing = ttk.LabelFrame(frame, text="One-time pairing code", padding=16)
        pairing.pack(fill="x")
        ttk.Label(pairing, textvariable=self.code, font=("Menlo", 24, "bold")).pack()
        ttk.Label(
            pairing,
            text="Enter this code in ADD within five minutes. The code never authorizes a flash by itself.",
            wraplength=420,
            justify="center",
        ).pack(pady=(8, 0))
        ttk.Checkbutton(
            frame,
            text="Start this companion when I sign in",
            variable=self.startup,
            command=self._toggle_startup,
        ).pack(anchor="w", pady=18)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="Open ADD", command=lambda: webbrowser.open(DASHBOARD)).pack(
            side="left"
        )
        ttk.Button(actions, text="Retry connection", command=self._retry).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Quit", command=root.destroy).pack(side="right")

        identity = load_or_create_identity()
        self.client = CompanionClient(identity, self._notify)
        threading.Thread(target=self._run_client, daemon=True).start()
        root.after(100, self._drain)

    def _notify(self, kind: str, payload: dict) -> None:
        self.messages.put((kind, payload))

    def _run_client(self) -> None:
        asyncio.run(self.client.run())

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "pairing":
                code = str(payload["pairing_code"])
                self.code.set(" ".join(code))
                self.status.set("Pair this companion in ADD")
            elif kind == "connected":
                if payload["online"]:
                    self.code.set("Paired")
                    self.status.set("Connected securely to ADD")
            elif kind == "stage":
                self.status.set(payload["text"])
            elif kind == "device":
                self.usb.set(f"ESP32-S3 detected · Wi-Fi MAC {payload['hardware_mac']}")
            elif kind == "device_selection":
                self._choose_device(payload.get("candidates", []))
            elif kind == "progress":
                self.status.set(payload["state"].replace("_", " ").title())
            elif kind == "error":
                self.status.set("Attention required")
                self.usb.set(payload["text"])
        self.root.after(100, self._drain)

    def _choose_device(self, candidates: list[dict]) -> None:
        chooser = tk.Toplevel(self.root)
        chooser.title("Choose one ESP32-S3")
        chooser.transient(self.root)
        chooser.grab_set()
        body = ttk.Frame(chooser, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Several valid ESP32-S3 devices are connected. Choose by Wi-Fi MAC and USB identity.",
            wraplength=420,
        ).pack(anchor="w", pady=(0, 12))
        selection = tk.StringVar(value=candidates[0]["port"] if candidates else "")
        for candidate in candidates:
            usb = candidate.get("usb_identity", {})
            label = (
                f"{candidate['hardware_mac']} · {usb.get('manufacturer') or 'USB device'} "
                f"({usb.get('vid', 0):04x}:{usb.get('pid', 0):04x})"
            )
            ttk.Radiobutton(
                body, text=label, value=candidate["port"], variable=selection
            ).pack(anchor="w", pady=5)

        def choose() -> None:
            if selection.get():
                self.client.select_port(selection.get())
                chooser.destroy()

        ttk.Button(body, text="Use selected ESP32-S3", command=choose).pack(
            anchor="e", pady=(16, 0)
        )

    def _toggle_startup(self) -> None:
        try:
            set_login_start(self.startup.get())
            self.status.set(
                "Login start enabled." if self.startup.get() else "Login start disabled."
            )
        except Exception:
            self.startup.set(False)
            self.status.set("Login start could not be changed.")

    def _retry(self) -> None:
        self.status.set("Retrying secure ADD connection…")
        self.client.request_retry()


def main() -> None:
    if platform.system() not in {"Windows", "Darwin"}:
        raise SystemExit("Supported platforms are Windows x64 and macOS Apple Silicon.")
    root = tk.Tk()
    CompanionWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
