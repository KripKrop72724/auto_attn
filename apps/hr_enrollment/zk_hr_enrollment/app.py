from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from zk_hr_enrollment import APP_NAME, BRAND_NAME
from zk_hr_enrollment.identity import FINGER_LABELS
from zk_hr_enrollment.service import EmployeeRecord, HREnrollmentService
from zk_hr_enrollment.zkt import ScannedDevice


class HREnrollmentApp(tk.Tk):
    def __init__(self, service: HREnrollmentService | None = None) -> None:
        super().__init__()
        self.service = service or HREnrollmentService()
        self.devices: dict[str, ScannedDevice] = {}
        self.current_record: EmployeeRecord | None = None
        self.task_queue: queue.Queue = queue.Queue()

        self.title(f"{BRAND_NAME} - {APP_NAME}")
        self.geometry("900x650")
        self.minsize(820, 600)

        self.device_var = tk.StringVar()
        self.cnic_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.shift_var = tk.BooleanVar()
        self.finger_var = tk.StringVar(value=_finger_option(4))
        self.finger_summary_var = tk.StringVar(value="No employee selected")

        self._build_ui()
        self._set_busy(False)
        self.after(100, self._poll_tasks)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Brand.TLabel", font=("Segoe UI", 11))
        style.configure("Action.TButton", padding=(12, 6))

        root = ttk.Frame(self, padding=18)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(header, text=BRAND_NAME, style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(header, text="Employee fingerprint enrollment", style="Brand.TLabel").pack(anchor=tk.W)

        device_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Device Selection", padding=12)
        device_frame.pack(fill=tk.X, pady=(18, 10))
        ttk.Button(
            device_frame,
            text="Scan Network",
            style="Action.TButton",
            command=self.scan_devices,
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.scan_button = device_frame.grid_slaves(row=0, column=0)[0]
        self.device_combo = ttk.Combobox(
            device_frame,
            textvariable=self.device_var,
            state="readonly",
            width=84,
        )
        self.device_combo.grid(row=0, column=1, sticky=tk.EW)
        device_frame.columnconfigure(1, weight=1)

        employee_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Employee", padding=12)
        employee_frame.pack(fill=tk.X, pady=10)
        ttk.Label(employee_frame, text="CNIC").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(employee_frame, textvariable=self.cnic_var, width=28).grid(
            row=0, column=1, sticky=tk.W, pady=4, padx=(8, 18)
        )
        ttk.Label(employee_frame, text="Full Name").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(employee_frame, textvariable=self.name_var, width=54).grid(
            row=1, column=1, columnspan=3, sticky=tk.EW, pady=4, padx=(8, 0)
        )
        ttk.Checkbutton(employee_frame, text="Shift worker", variable=self.shift_var).grid(
            row=0, column=2, sticky=tk.W, pady=4
        )
        self.search_button = ttk.Button(
            employee_frame,
            text="Search Employee",
            style="Action.TButton",
            command=self.search_employee,
        )
        self.search_button.grid(row=2, column=1, sticky=tk.W, pady=(10, 0), padx=(8, 8))
        self.create_button = ttk.Button(
            employee_frame,
            text="Create Employee",
            style="Action.TButton",
            command=self.create_employee,
        )
        self.create_button.grid(row=2, column=2, sticky=tk.W, pady=(10, 0), padx=(0, 8))
        employee_frame.columnconfigure(3, weight=1)

        finger_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Finger Enrollment", padding=12)
        finger_frame.pack(fill=tk.X, pady=10)
        ttk.Label(finger_frame, text="Finger").grid(row=0, column=0, sticky=tk.W)
        self.finger_combo = ttk.Combobox(
            finger_frame,
            textvariable=self.finger_var,
            values=[_finger_option(fid) for fid in FINGER_LABELS],
            state="readonly",
            width=28,
        )
        self.finger_combo.grid(row=0, column=1, sticky=tk.W, padx=(8, 18))
        self.enroll_button = ttk.Button(
            finger_frame,
            text="Enroll Selected Finger",
            style="Action.TButton",
            command=self.enroll_finger,
        )
        self.enroll_button.grid(row=0, column=2, sticky=tk.W)
        ttk.Label(finger_frame, textvariable=self.finger_summary_var).grid(
            row=1, column=0, columnspan=3, sticky=tk.W, pady=(10, 0)
        )

        status_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Status", padding=12)
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.status_text = scrolledtext.ScrolledText(status_frame, height=12, wrap=tk.WORD, state="disabled")
        self.status_text.pack(fill=tk.BOTH, expand=True)
        self._append_status(f"{BRAND_NAME} HR enrollment is ready.")

    def scan_devices(self) -> None:
        self.current_record = None
        self.finger_summary_var.set("No employee selected")
        self._run_task("Scanning network for ZKT devices", self.service.scan_devices, self._on_scan_complete)

    def search_employee(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        cnic = self.cnic_var.get()
        self._run_task(
            "Searching employee on selected device",
            lambda: self.service.search_employee(device, cnic),
            self._on_search_complete,
        )

    def create_employee(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        self._run_task(
            "Creating regular employee on selected device",
            lambda: self.service.create_employee(
                device,
                full_name=self.name_var.get(),
                cnic=self.cnic_var.get(),
                shift_worker=self.shift_var.get(),
            ),
            self._on_create_complete,
        )

    def enroll_finger(self) -> None:
        device = self._selected_device()
        if device is None or self.current_record is None:
            messagebox.showerror(BRAND_NAME, "Search or create an employee before enrollment.")
            return
        finger_id = _selected_finger_id(self.finger_var.get())
        self._run_task(
            "Waiting for fingerprint enrollment on the device",
            lambda: self.service.enroll_finger(device, record=self.current_record, finger_id=finger_id),
            self._on_enroll_complete,
        )

    def _on_scan_complete(self, devices: list[ScannedDevice]) -> None:
        self.devices = {device.label: device for device in devices}
        self.device_combo["values"] = list(self.devices)
        if devices:
            self.device_var.set(devices[0].label)
            self._append_status(f"Found {len(devices)} ZKT device(s). Select one to continue.")
        else:
            self.device_var.set("")
            self._append_status("No compatible ZKT devices were found on the connected network.")

    def _on_search_complete(self, result) -> None:
        self.current_record = result.record if result.found else None
        self._append_status(result.message)
        if result.record:
            self._set_record(result.record)
        else:
            self.finger_summary_var.set("Employee not found")

    def _on_create_complete(self, record: EmployeeRecord) -> None:
        self._set_record(record)
        self._append_status(f"Created regular employee {record.machine_name}. Select a finger to enroll.")

    def _on_enroll_complete(self, outcome) -> None:
        self._set_record(outcome.record)
        self._append_status(outcome.message)
        self._append_status(f"Verified fingers: {outcome.record.finger_summary}.")

    def _set_record(self, record: EmployeeRecord) -> None:
        self.current_record = record
        self.finger_summary_var.set(
            f"Device user {record.user_id}: {record.machine_name} | {record.finger_summary}"
        )

    def _selected_device(self) -> ScannedDevice | None:
        label = self.device_var.get()
        device = self.devices.get(label)
        if device is None:
            messagebox.showerror(BRAND_NAME, "Select a ZKT device first.")
            return None
        return device

    def _run_task(self, label: str, func, on_success) -> None:
        self._append_status(f"{label}...")
        self._set_busy(True)

        def worker() -> None:
            try:
                self.task_queue.put(("success", label, func(), on_success))
            except Exception as exc:
                self.task_queue.put(("error", label, exc, None))

        threading.Thread(target=worker, name=f"hr-enrollment-{label}", daemon=True).start()

    def _poll_tasks(self) -> None:
        try:
            while True:
                kind, label, payload, callback = self.task_queue.get_nowait()
                self._set_busy(False)
                if kind == "success":
                    callback(payload)
                else:
                    self._append_status(f"{label} failed: {payload}")
                    messagebox.showerror(BRAND_NAME, str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll_tasks)

    def _set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (self.scan_button, self.search_button, self.create_button, self.enroll_button):
            button.configure(state=state)

    def _append_status(self, message: str) -> None:
        self.status_text.configure(state="normal")
        self.status_text.insert(tk.END, f"{message}\n")
        self.status_text.see(tk.END)
        self.status_text.configure(state="disabled")


def run_app() -> None:
    app = HREnrollmentApp()
    app.mainloop()


def _finger_option(fid: int) -> str:
    return f"{fid} - {FINGER_LABELS[fid]}"


def _selected_finger_id(value: str) -> int:
    return int(value.split(" ", 1)[0])
