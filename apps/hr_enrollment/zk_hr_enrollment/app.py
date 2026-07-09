from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from zk_hr_enrollment import APP_NAME, BRAND_NAME
from zk_hr_enrollment.diagnostics import (
    friendly_exception_message,
    log_exception,
    message_with_log,
)
from zk_hr_enrollment.identity import FINGER_LABELS
from zk_hr_enrollment.service import EmployeeRecord, HREnrollmentService
from zk_hr_enrollment.zkt import ScannedDevice


AUTO_SCAN_INITIAL_DELAY_MS = 1000
AUTO_SCAN_INTERVAL_MS = 30000


class HREnrollmentApp(tk.Tk):
    def __init__(self, service: HREnrollmentService | None = None) -> None:
        super().__init__()
        self.service = service or HREnrollmentService()
        self.devices: dict[str, ScannedDevice] = {}
        self.current_record: EmployeeRecord | None = None
        self.task_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.scan_busy = False
        self.last_scan_signature: tuple[str, ...] | None = None

        self.title(f"{BRAND_NAME} - {APP_NAME}")
        self.geometry("900x650")
        self.minsize(820, 600)
        self.report_callback_exception = self._handle_callback_exception

        self.device_var = tk.StringVar()
        self.cnic_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.shift_var = tk.BooleanVar()
        self.finger_var = tk.StringVar(value=_finger_option(4))
        self.finger_summary_var = tk.StringVar(value="No employee selected")

        self._build_ui()
        self._set_busy(False)
        self.after(100, self._poll_tasks)
        self.after(AUTO_SCAN_INITIAL_DELAY_MS, self._auto_scan_devices)

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
        ttk.Label(header, text="Employee biometric enrollment", style="Brand.TLabel").pack(anchor=tk.W)

        device_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Device Selection", padding=12)
        device_frame.pack(fill=tk.X, pady=(18, 10))
        self.scan_button = ttk.Button(
            device_frame,
            text="Scan Network",
            style="Action.TButton",
            command=self.scan_devices,
        )
        self.scan_button.grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.device_combo = ttk.Combobox(
            device_frame,
            textvariable=self.device_var,
            state="readonly",
            width=84,
        )
        self.device_combo.grid(row=0, column=1, sticky=tk.EW)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_changed)
        device_frame.columnconfigure(1, weight=1)

        employee_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Employee", padding=12)
        employee_frame.pack(fill=tk.X, pady=10)
        ttk.Label(employee_frame, text="CNIC").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.cnic_entry = ttk.Entry(employee_frame, textvariable=self.cnic_var, width=28)
        self.cnic_entry.grid(row=0, column=1, sticky=tk.W, pady=4, padx=(8, 18))
        ttk.Label(employee_frame, text="Full Name").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.name_entry = ttk.Entry(employee_frame, textvariable=self.name_var, width=54)
        self.name_entry.grid(row=1, column=1, columnspan=3, sticky=tk.EW, pady=4, padx=(8, 0))
        self.shift_check = ttk.Checkbutton(employee_frame, text="Shift worker", variable=self.shift_var)
        self.shift_check.grid(row=0, column=2, sticky=tk.W, pady=4)
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

        finger_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Biometric Enrollment", padding=12)
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
        self.enroll_button.grid(row=0, column=2, sticky=tk.W, padx=(0, 8))
        self.face_button = ttk.Button(
            finger_frame,
            text="Enroll Face",
            style="Action.TButton",
            command=self.enroll_face,
        )
        self.face_button.grid(row=0, column=3, sticky=tk.W)
        ttk.Label(finger_frame, textvariable=self.finger_summary_var).grid(
            row=1, column=0, columnspan=4, sticky=tk.W, pady=(10, 0)
        )

        status_frame = ttk.LabelFrame(root, text=f"{BRAND_NAME} - Status", padding=12)
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.status_text = scrolledtext.ScrolledText(status_frame, height=12, wrap=tk.WORD, state="disabled")
        self.status_text.pack(fill=tk.BOTH, expand=True)
        self._append_status(f"{BRAND_NAME} HR enrollment is ready.")

    def scan_devices(self) -> None:
        self._start_device_scan(manual=True)

    def _auto_scan_devices(self) -> None:
        self._start_device_scan(manual=False)
        self.after(AUTO_SCAN_INTERVAL_MS, self._auto_scan_devices)

    def _start_device_scan(self, *, manual: bool) -> None:
        if self.busy:
            if manual:
                self._append_status("Finish the current operation before scanning again.")
            return
        if self.scan_busy:
            if manual:
                self._append_status("A network scan is already running.")
            return
        if manual:
            self.last_scan_signature = None
            self._clear_scan_selection()
        label = "Scanning network for ZKT devices"
        if manual or self.last_scan_signature is None:
            self._append_status(f"{label}...")
        self._set_scan_busy(True)

        def on_success(devices: list[ScannedDevice]) -> None:
            self._on_scan_complete(devices, manual=manual)

        def worker() -> None:
            try:
                self.task_queue.put(("scan_success", label, self.service.scan_devices(), on_success))
            except Exception as exc:
                self.task_queue.put(("scan_error", label, exc, manual))

        threading.Thread(target=worker, name="hr-enrollment-network-scan", daemon=True).start()

    def _clear_scan_selection(self) -> None:
        self.current_record = None
        self.devices = {}
        self.device_var.set("")
        self.device_combo["values"] = []
        self.finger_summary_var.set("No employee selected")

    def search_employee(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        cnic = self.cnic_var.get()
        self._clear_record("Searching employee")
        self._run_task(
            "Searching employee on selected device",
            lambda: self.service.search_employee(device, cnic),
            self._on_search_complete,
        )

    def create_employee(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        full_name = self.name_var.get()
        cnic = self.cnic_var.get()
        shift_worker = self.shift_var.get()
        self._clear_record("Creating employee")
        self._run_task(
            "Creating regular employee on selected device",
            lambda: self.service.create_employee(
                device,
                full_name=full_name,
                cnic=cnic,
                shift_worker=shift_worker,
            ),
            self._on_create_complete,
        )

    def enroll_finger(self) -> None:
        device = self._selected_device()
        if device is None or self.current_record is None:
            messagebox.showerror(BRAND_NAME, "Search or create an employee before enrollment.")
            return
        record = self.current_record
        try:
            finger_id = _selected_finger_id(self.finger_var.get())
        except ValueError:
            messagebox.showerror(BRAND_NAME, "Finger selection is invalid.")
            return
        self._run_task(
            "Waiting for fingerprint enrollment on the device",
            lambda: self.service.enroll_finger(device, record=record, finger_id=finger_id),
            self._on_enroll_complete,
        )

    def enroll_face(self) -> None:
        device = self._selected_device()
        if device is None or self.current_record is None:
            messagebox.showerror(BRAND_NAME, "Search or create an employee before enrollment.")
            return
        record = self.current_record
        self._run_task(
            "Waiting for face enrollment on the device",
            lambda: self.service.enroll_face(device, record=record),
            self._on_enroll_complete,
        )

    def _on_scan_complete(self, devices: list[ScannedDevice], *, manual: bool = True) -> None:
        previous_label = self.device_var.get()
        previous_record = self.current_record
        labels = tuple(device.label for device in devices)
        changed = labels != self.last_scan_signature
        self.last_scan_signature = labels
        self.devices = {device.label: device for device in devices}
        self.device_combo["values"] = list(self.devices)
        if devices:
            if previous_label in self.devices:
                self.device_var.set(previous_label)
            else:
                self.device_var.set(devices[0].label)
            validated = sum(1 for device in devices if device.validated)
            if previous_record is not None and previous_label and previous_label != self.device_var.get():
                self._clear_record("Selected device changed; search or create the employee again")
            if validated == len(devices) and (manual or changed):
                self._append_status(f"Found {len(devices)} ZKT device(s). Select one to continue.")
            elif manual or changed:
                self._append_status(
                    f"Found {len(devices)} open ZKT port candidate(s); {validated} validated now. "
                    "Unvalidated candidates can still be selected and will be tried with TCP then UDP."
                )
        else:
            self.device_var.set("")
            if previous_record is not None and previous_label:
                self._clear_record("Selected device is no longer visible; search or create the employee again")
            if manual or changed:
                self._append_status("No open ZKT port candidates were found on the connected network.")

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
        if getattr(outcome, "modality", "fingerprint") == "face":
            self._append_status(f"Current fingers: {outcome.record.finger_summary}.")
        else:
            self._append_status(f"Verified fingers: {outcome.record.finger_summary}.")

    def _set_record(self, record: EmployeeRecord) -> None:
        self.current_record = record
        self.finger_summary_var.set(
            f"Device user {record.user_id}: {record.machine_name} | {record.finger_summary}"
        )

    def _clear_record(self, reason: str = "No employee selected") -> None:
        self.current_record = None
        self.finger_summary_var.set(reason)

    def _on_device_changed(self, _event=None) -> None:
        self._clear_record("Search or create an employee on the selected device")
        device = self.devices.get(self.device_var.get())
        if device is not None and not device.validated:
            self._append_status(
                f"Selected {device.ip}:{device.port}. It was found by port scan and will be validated "
                "when the next command runs."
            )

    def _selected_device(self) -> ScannedDevice | None:
        label = self.device_var.get()
        device = self.devices.get(label)
        if device is None:
            messagebox.showerror(BRAND_NAME, "Select a ZKT device first.")
            return None
        return device

    def _run_task(self, label: str, func, on_success) -> None:
        if self.busy:
            self._append_status("Another operation is already running.")
            return
        if self.scan_busy:
            self._append_status("A network scan is finishing. Try again in a moment.")
            return
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
                if kind == "scan_success":
                    self._set_scan_busy(False)
                    callback(payload)
                elif kind == "scan_error":
                    self._set_scan_busy(False)
                    log_path = log_exception(label, payload)
                    message = friendly_exception_message(payload)
                    self._append_status(f"{label} failed: {message}")
                    if callback:
                        messagebox.showerror(BRAND_NAME, message_with_log(message, log_path))
                else:
                    self._set_busy(False)
                    if kind == "success":
                        callback(payload)
                    else:
                        log_path = log_exception(label, payload)
                        message = friendly_exception_message(payload)
                        self._append_status(f"{label} failed: {message}")
                        messagebox.showerror(BRAND_NAME, message_with_log(message, log_path))
        except queue.Empty:
            pass
        self.after(100, self._poll_tasks)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._refresh_control_states()

    def _set_scan_busy(self, busy: bool) -> None:
        self.scan_busy = busy
        self._refresh_control_states()

    def _refresh_control_states(self) -> None:
        action_blocked = self.busy or self.scan_busy
        action_state = tk.DISABLED if action_blocked else tk.NORMAL
        self.scan_button.configure(
            state=action_state,
            text="Scanning..." if self.scan_busy else "Scan Network",
        )
        for button in (self.search_button, self.create_button, self.enroll_button, self.face_button):
            button.configure(state=action_state)
        combo_state = tk.DISABLED if action_blocked else "readonly"
        for combo in (self.device_combo, self.finger_combo):
            combo.configure(state=combo_state)
        entry_state = tk.DISABLED if self.busy else tk.NORMAL
        for widget in (self.cnic_entry, self.name_entry, self.shift_check):
            widget.configure(state=entry_state)

    def _append_status(self, message: str) -> None:
        self.status_text.configure(state="normal")
        self.status_text.insert(tk.END, f"{message}\n")
        self.status_text.see(tk.END)
        self.status_text.configure(state="disabled")

    def _handle_callback_exception(self, exc_type, exc, tb) -> None:
        log_path = log_exception("tk callback", exc, tb)
        message = friendly_exception_message(exc)
        self._append_status(f"Operation failed: {message}")
        messagebox.showerror(BRAND_NAME, message_with_log(message, log_path))


def run_app() -> None:
    app = HREnrollmentApp()
    app.mainloop()


def _finger_option(fid: int) -> str:
    return f"{fid} - {FINGER_LABELS[fid]}"


def _selected_finger_id(value: str) -> int:
    return int(value.split(" ", 1)[0])
