#!/usr/bin/env python3
"""Export every code the dashboard can show, by words domain.

The dashboard turns codes into plain words with the dictionaries in
apps/add_frontend/src/words/. This script lists every code those dictionaries
must cover, read straight from the backend source (Python AST, nothing is
imported) and the link box software source (C, by pattern; nothing is built).

Each domain lists its codes under "backend" (the dashboard writes or stores
them; words are required, the words test fails without them) and "firmware"
(only the link box software sends them; per owner decision D-22 a firmware-only
code may use its group's fallback sentence and is listed as a follow-up).

Usage:
    python scripts/export_word_codes.py          # rewrite both JSON copies
    python scripts/export_word_codes.py --check  # exit 1 when they are stale

Outputs (identical content):
    apps/add_frontend/src/words/codes.generated.json  (read only by words tests)
    apps/add_backend/zk_add/word_codes.json           (read by backend word tests)
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps" / "add_backend" / "zk_add"
FIRMWARE = ROOT / "firmware" / "zone_lite" / "main"
OUTPUTS = (
    ROOT / "apps" / "add_frontend" / "src" / "words" / "codes.generated.json",
    BACKEND / "word_codes.json",
)
CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^[A-Z]{3,}$")

# Codes only old link box software sends; they are not in today's source.
LEGACY_CODES = {"log": ["BLOCKED_IDENTITY_RECOVERY_DEFERRED"]}

# Domain -> short description, so a person reading the JSON knows where each
# list ends up on screen. The frontend words test maps each domain to its
# dictionary function.
DOMAINS = {
    "device": "Device status (Connector.lifecycle_state)",
    "tier": "Health tier of one reason",
    "machineLink": "Machine connection state (terminal_link.state)",
    "machineLinkReason": "Machine connection reason (terminal_link.reason)",
    "machineRaw": "Machine connection history state (connection_state)",
    "activity": "What the device is doing now (current_activity)",
    "problem": "Problem kinds (POLICY codes)",
    "severity": "How urgent a problem is",
    "refusal": "Why a problem can't be marked as fixed by hand",
    "delivery": "Sending a check-in to the HR system (ords_status)",
    "identity": "Matching a check-in to an employee",
    "requestType": "Request sent to a device (command_type)",
    "request": "Request status (command status)",
    "requestFailure": "Why a request failed (command error_code)",
    "peopleRemoval": "Removing several people (deletion job status)",
    "writesOff": "Why adding or changing people is off (writes_disabled_reason)",
    "lease": "Temporary admin access state",
    "commKey": "Machine password (COMM Key) state",
    "commKeyError": "Why a machine password change failed",
    "softwareDevice": "Device update state (ota_state)",
    "update": "Update to one device (deployment status)",
    "updateEvent": "Update timeline event (FirmwareEvent.state)",
    "rollout": "Update rollout state (campaign status)",
    "updateCode": "Codes the link box reports during an update",
    "recoverJob": "Recover attendance job status",
    "recoverPhase": "Recover attendance stage",
    "recoverStep": "What a Recover attendance job is doing",
    "forceReason": "Force release reasons",
    "directReason": "Send saved check-ins reasons",
    "error": "Refusals (HTTP 409 code)",
    "log": "Device activity log codes",
}


# ---------------------------------------------------------------- helpers

def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@lru_cache(maxsize=None)
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@lru_cache(maxsize=None)
def _nodes(path: Path) -> tuple:
    return tuple(ast.walk(_tree(path)))


def _strings(node: ast.AST | None) -> list[str]:
    """Every string constant inside an expression (sets, tuples, ternaries...)."""
    if node is None:
        return []
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _values(node: ast.AST | None) -> list[str]:
    """String values an expression can produce (not the strings in a ternary's test)."""
    if node is None:
        return []
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.IfExp):
        return _values(node.body) + _values(node.orelse)
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return [v for elt in node.elts for v in _values(elt)]
    if isinstance(node, ast.Call):  # frozenset({...}), set(...)
        return [v for arg in node.args for v in _values(arg)]
    if isinstance(node, ast.BinOp):  # A | frozenset({...})
        return _values(node.left) + _values(node.right)
    if isinstance(node, ast.Dict):
        return [v for key in node.keys for v in _values(key)] + [v for val in node.values for v in _values(val)]
    if isinstance(node, ast.Subscript):  # Literal["A", "B"]
        return _values(node.slice)
    return []


def _codes(values) -> set[str]:
    return {v for v in values if isinstance(v, str) and CODE.match(v)}


def assigned(path: Path, *names: str, keys_only: bool = False) -> set[str]:
    """String constants assigned to module-level (or class-level) names."""
    found: set[str] = set()
    for node in _nodes(path):
        targets: list[ast.AST] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
                for name_node, item in zip(target.elts, value.elts):
                    if isinstance(name_node, ast.Name) and name_node.id in names:
                        found |= _codes(_values(item))
                continue
            if isinstance(target, ast.Name) and target.id in names:
                if keys_only and isinstance(value, ast.Dict):
                    found |= _codes(_strings(k) and _strings(k)[0] for k in value.keys if k is not None)
                elif value is not None:
                    found |= _codes(_values(value))
                if keys_only is False and isinstance(node, ast.AnnAssign):
                    found |= _codes(_values(node.annotation))
    return found


def dict_keys(path: Path, name: str) -> set[str]:
    found: set[str] = set()
    for node in _nodes(path):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.add(key.value)
    return _codes(found)


def dict_values(path: Path, name: str) -> set[str]:
    found: set[str] = set()
    for node in _nodes(path):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    found |= _codes(_values(value))
    return found


def attribute_assignments(path: Path, attribute: str, owner: str | None = None) -> set[str]:
    """`x.<attribute> = "CODE"` (also chained `a = x.attr = "CODE"`) and `<attribute>="CODE"` keywords."""
    found: set[str] = set()
    for node in _nodes(path):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == attribute:
                    if owner is None or (isinstance(target.value, ast.Name) and target.value.id == owner):
                        if isinstance(node.value, (ast.Constant, ast.IfExp)):
                            found |= _codes(_values(node.value))
        elif isinstance(node, ast.keyword) and node.arg == attribute and owner is None:
            if isinstance(node.value, (ast.Constant, ast.IfExp)):
                found |= _codes(_values(node.value))
    return found


def call_arguments(path: Path, function: str, position: int | None = None, keyword: str | None = None) -> set[str]:
    """String constants passed to `function(...)` at a position or keyword."""
    found: set[str] = set()
    for node in _nodes(path):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name != function:
            continue
        if position is not None and len(node.args) > position:
            arg = node.args[position]
            if isinstance(arg, (ast.Constant, ast.IfExp)):
                found |= _codes(_values(arg))
        if keyword is not None:
            for kw in node.keywords:
                if kw.arg == keyword and isinstance(kw.value, (ast.Constant, ast.IfExp)):
                    found |= _codes(_values(kw.value))
    return found


def function_returns(path: Path, function: str) -> set[str]:
    """Codes in the first element of tuples (or plain strings) a function returns."""
    found: set[str] = set()
    for node in _nodes(path):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    value = ret.value
                    if isinstance(value, ast.Tuple) and value.elts:
                        value = value.elts[0]
                    found |= _codes(_values(value) if isinstance(value, (ast.Constant, ast.IfExp)) else [])
    return found


def class_literal_field(path: Path, cls: str, field: str) -> set[str]:
    for node in _nodes(path):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == field:
                    return _codes(_values(item.annotation))
    return set()


def backend_files() -> list[Path]:
    return sorted(BACKEND.glob("*.py"))


# ----------------------------------------------------------- C extraction

_C_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _c_calls(text: str, function: str):
    """Yield the argument list (as source strings) of every call to `function`."""
    for match in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(function) + r"\s*\(", text):
        start = match.end()
        depth, index, args, current, in_string = 1, start, [], [], False
        while index < len(text) and depth:
            char = text[index]
            if in_string:
                current.append(char)
                if char == "\\":
                    current.append(text[index + 1])
                    index += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
                current.append(char)
            elif char in "([{":
                depth += 1
                current.append(char)
            elif char in ")]}":
                depth -= 1
                if depth:
                    current.append(char)
            elif char == "," and depth == 1:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(char)
            index += 1
        args.append("".join(current).strip())
        # Skip the definition / prototype (first argument is a C type).
        if args and re.match(r"^(const\s+)?(char|bool|int|void|size_t|[a-z_]+_t)\b", args[0]):
            continue
        yield args


def _literal_codes(argument: str) -> set[str]:
    return _codes(_C_STRING.findall(argument))


def firmware_texts() -> dict[Path, str]:
    return {path: _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            for path in sorted(FIRMWARE.glob("*.c"))}


LOG_HELPERS = {
    # function: index of the code argument
    "add_connector_log": 2,
    "add_connector_log_acknowledged": 2,
    "logf_line": 2,
    "say": 2,
    "queue_line": 1,
}


def firmware_log_codes(texts: dict[Path, str]) -> set[str]:
    found: set[str] = set()
    for text in texts.values():
        for function, position in LOG_HELPERS.items():
            for args in _c_calls(text, function):
                if len(args) > position:
                    found |= _literal_codes(args[position])
    zone = texts.get(FIRMWARE / "zone_lite.c", "")
    # Frame codes chosen from a static array, logged through a variable.
    for block in re.findall(r"static\s+const\s+char\s*\*\s*const\s+codes\[[A-Z_0-9]*\]\s*=\s*\{([^}]*)\}", zone):
        found |= _literal_codes(block)
    # Storage repair end codes picked by a ternary before logging.
    ota = texts.get(FIRMWARE / "ota_manager.c", "")
    for name in ("STORAGE_REPAIR_EXCEPTION_UNHEALTHY", "STORAGE_REPAIR_EXCEPTION_UNREPAIRED"):
        if f'"{name}"' in ota:
            found.add(name)
    # The engineer-test code family is formatted at run time ("ZKT_DIAG_%s").
    found = {code for code in found if "%" not in code}
    return found


def firmware_activity(texts: dict[Path, str]) -> tuple[set[str], set[str]]:
    activity: set[str] = set()
    raw: set[str] = set()
    for text in texts.values():
        for args in _c_calls(text, "add_connector_set_activity"):
            activity |= _literal_codes(args[0])
        for args in _c_calls(text, "zkt_publish_state"):
            raw |= _literal_codes(args[0])
        for match in re.finditer(r'strlcpy\(\s*s_activity\s*,\s*"([A-Z0-9_]+)"', text):
            activity.add(match.group(1))
    return activity | raw, raw


def firmware_update_codes(texts: dict[Path, str]) -> set[str]:
    found: set[str] = set()
    ota = texts.get(FIRMWARE / "ota_manager.c", "")
    for function in ("report_state", "report_progress"):
        for args in _c_calls(ota, function):
            if len(args) > 1:
                found |= _literal_codes(args[1])
    found |= _codes(re.findall(r'strlcpy\(\s*s_last_error\s*,\s*"([A-Z0-9_]+)"', ota))
    connector = texts.get(FIRMWARE / "add_connector.c", "")
    found |= _codes(re.findall(r'return\s+"(BOOT_LOCAL_[A-Z0-9_]+)"', connector))
    compat = texts.get(FIRMWARE / "zkt_journal_compat.c", "")
    found |= _codes(re.findall(r'case\s+ZJ_[A-Z0-9_]+\s*:\s*return\s+"([A-Z0-9_]+)"', compat))
    recovery = texts.get(FIRMWARE / "storage_recovery.c", "")
    for function in ("finish", "refuse"):
        for args in _c_calls(recovery, function):
            for arg in args:
                found |= {c for c in _literal_codes(arg) if c.startswith("STORAGE_RECOVERY_")}
    found |= {c for c in _codes(re.findall(r'"(STORAGE_RECOVERY_[A-Z0-9_]+)"', recovery))}
    factory = texts.get(FIRMWARE / "zkt_factory_platform.c", "")
    found |= _codes(re.findall(r'"(FACTORY_TRIAL_[A-Z0-9_]+)"', factory))
    return found


def firmware_command_failures(texts: dict[Path, str]) -> set[str]:
    found: set[str] = set()
    for text in texts.values():
        for args in _c_calls(text, "add_connector_command_update"):
            if len(args) > 2 and _literal_codes(args[1]) & {"FAILED", "RETRYING", "EXPIRED"}:
                found |= _literal_codes(args[2])
        found |= _codes(re.findall(r'error_code\s*=\s*"([A-Z0-9_]+)"', text))
    return found


def firmware_comm_key_errors(texts: dict[Path, str]) -> set[str]:
    zone = texts.get(FIRMWARE / "zone_lite.c", "")
    found = _codes(re.findall(r'"(COMM_KEY_[A-Z0-9_]+)"', zone))
    # Probe stage codes reported with a failed machine password check.
    found |= _codes(re.findall(r'"((?:TCP_4370|CONNECT|AUTH|SERIAL)_[A-Z0-9_]+)"', zone))
    return {code for code in found if code != "COMM_KEY_APPLIED"}


# --------------------------------------------------------------- backend

def backend_domains() -> dict[str, set[str]]:
    b = BACKEND
    out: dict[str, set[str]] = {name: set() for name in DOMAINS}

    out["device"] |= attribute_assignments(b / "service.py", "lifecycle_state", owner="connector")
    for path in backend_files():
        out["device"] |= attribute_assignments(path, "lifecycle_state", owner="connector")
    out["device"] |= {"ONBOARDING", "QUARANTINED_DUPLICATE_SERIAL", "ONLINE_WITH_WARNINGS"} & set(
        re.findall(r'"([A-Z_]+)"', (b / "device_health.py").read_text()))

    out["tier"] |= assigned(b / "device_health.py", "DEGRADED", "WARNING")
    for name, call in (("machineLink", 0), ("machineLinkReason", 3)):
        out[name] |= call_arguments(b / "device_health.py", "_link", position=call)
    out["machineLinkReason"] |= dict_values(b / "device_health.py", "HIK_POLL_ERRORS")

    out["problem"] |= dict_keys(b / "device_health.py", "POLICY")
    for path in backend_files():
        out["severity"] |= {c for c in call_arguments(path, "upsert_alert", keyword="severity")}
        out["severity"] |= attribute_assignments(path, "severity", owner="alert")
    for path in backend_files():
        for node in _nodes(path):
            if isinstance(node, ast.keyword) and node.arg == "severity":
                out["severity"] |= _codes(_values(node.value) if isinstance(node.value, (ast.Constant, ast.IfExp)) else [])
    out["refusal"] |= call_arguments(b / "device_health.py", "refuse", position=0)

    out["delivery"] |= assigned(b / "ords_states.py", *[
        "ORDS_DELIVERY_ACTIVE_STATUSES", "ORDS_FIRMWARE_UNVERIFIED_STATUSES", "ORDS_MEMBERSHIP_REVERIFY_STATUSES",
        "ORDS_RECONCILIATION_PENDING_STATUSES", "ORDS_ACKNOWLEDGED_STATUSES", "ORDS_IDENTITY_HELD_STATUSES",
        "ORDS_TERMINAL_REVIEW_STATUSES"])
    for path in backend_files():
        out["delivery"] |= attribute_assignments(path, "ords_status")

    out["identity"] |= assigned(b / "identity_states.py", "PINNED_IDENTITY_RESOLUTION_STATUSES",
                                "VERIFIED_IDENTITY_RESOLUTION_STATUSES")
    for path in backend_files():
        out["identity"] |= attribute_assignments(path, "identity_resolution_status")

    out["request"] |= assigned(b / "service.py", "ACTIVE_COMMAND_STATES")
    out["request"] |= class_literal_field(b / "schemas.py", "CommandUpdate", "status")
    for path in backend_files():
        out["requestType"] |= call_arguments(path, "_queue_command", keyword="command_type")
        for node in _nodes(path):
            if isinstance(node, ast.keyword) and node.arg == "command_type" and isinstance(node.value, ast.Constant):
                out["requestType"] |= _codes([node.value.value])
    out["requestType"] |= assigned(b / "esp_stability.py", "ESP_REBOOT_COMMAND")

    out["peopleRemoval"] |= assigned(b / "service.py", "ACTIVE_USER_DELETION_JOB_STATES")
    service = (b / "service.py").read_text(encoding="utf-8")
    out["writesOff"] |= _codes(re.findall(r'writes_disabled_reason\W{1,6}"([A-Z0-9_]+)"', service))
    out["writesOff"] |= attribute_assignments(b / "service.py", "writes_disabled_reason")
    out["lease"] |= assigned(b / "reconciliation.py", "ACTIVE_LEASE_STATES")
    for path in backend_files():
        out["lease"] |= attribute_assignments(path, "state", owner="lease")

    comm = b / "comm_keys.py"
    out["commKey"] |= attribute_assignments(comm, "status", owner="state")
    for node in _nodes(comm):
        if isinstance(node, ast.keyword) and node.arg == "status" and isinstance(node.value, ast.Constant):
            out["commKey"] |= _codes([node.value.value])
    out["commKeyError"] |= attribute_assignments(comm, "last_error_code") | attribute_assignments(comm, "error_code")

    for path in backend_files():
        out["softwareDevice"] |= attribute_assignments(path, "ota_state")
    ota = b / "ota.py"
    out["update"] |= assigned(ota, "DEPLOYMENT_TRANSITIONS", "TERMINAL_DEPLOYMENT_STATES")
    for path in backend_files():
        out["updateEvent"] |= call_arguments(path, "FirmwareEvent", keyword="state")
    out["updateEvent"] |= _codes(
        v for name in ("DOWNLOAD_INTERRUPTED", "DOWNLOAD_RETRY_OFFERED", "DOWNLOAD_RETRIES_EXHAUSTED",
                       "DOWNLOAD_RETRY_NOT_TAKEN", "DOWNLOAD_RETRY_WITHDRAWN", "RETRY_REQUESTED")
        for v in assigned(b / "ota_interruption.py", name))
    out["updateEvent"] |= assigned(b / "bridge_observation.py", "EVENTS")
    out["rollout"] |= assigned(b / "firmware_campaigns.py", "LIVE_STATES", "CLOSED_STATES")
    out["updateCode"] |= dict_keys(b / "service.py", "OTA_FAILURE_MESSAGES")
    out["updateCode"] |= assigned(b / "service.py", "LOCAL_BOOT_WAIT_REASONS")
    out["updateCode"] |= assigned(ota, "STORAGE_EXCEPTION_CODE")

    recon = b / "reconciliation.py"
    out["recoverJob"] |= assigned(recon, "TERMINAL_JOB_STATES", "PAUSED_JOB_STATES")
    out["recoverJob"] |= attribute_assignments(recon, "status", owner="job")
    out["recoverPhase"] |= attribute_assignments(recon, "phase", owner="job")
    out["recoverStep"] |= function_returns(recon, "_operator_status")

    out["forceReason"] |= dict_keys(b / "attendance_force_release.py", "MESSAGES")
    out["directReason"] |= dict_keys(b / "attendance_direct_ords.py", "REASONS")

    for path in backend_files():
        out["error"] |= call_arguments(path, "RecoveryError", position=1, keyword="code")
    out["error"].add("RECOVERY_INVALID")

    # Log codes the dashboard writes itself.
    out["log"] |= _codes(re.findall(r'"(ADD_MESSAGE_PROCESSING_FAILED|DEVICE_MESSAGE_REJECTED)"',
                                    (b / "service.py").read_text()))
    out["log"] |= assigned(b / "source_tail_authority.py", "SOURCE_TAIL_COVERAGE_INACTIVE", "SOURCE_TAIL_EPOCH_STALE")
    return out


def firmware_domains() -> dict[str, set[str]]:
    texts = firmware_texts()
    out: dict[str, set[str]] = {name: set() for name in DOMAINS}
    out["log"] = firmware_log_codes(texts) | set(LEGACY_CODES["log"])
    out["activity"], out["machineRaw"] = firmware_activity(texts)
    out["updateCode"] = firmware_update_codes(texts)
    out["requestFailure"] = firmware_command_failures(texts)
    out["commKeyError"] = firmware_comm_key_errors(texts)
    return out


def _log_classes(codes: set[str]) -> dict[str, dict]:
    """Group, kind and act for each log code, from be/activity_words_data.py WORDS (04-device DEV-1)."""
    path = BACKEND / "activity_words_data.py"
    if not path.exists():
        return {}
    words = None
    for node in _tree(path).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "WORDS" for t in targets) and node.value is not None:
                words = ast.literal_eval(node.value)
    if not words:
        return {}
    classes = {}
    for code in sorted(codes):
        entry = words.get(code)
        if entry is None:
            continue
        value = {"group": entry["group"], "kind": entry["kind"], "act": entry["act"]}
        if entry.get("kind_by_level"):
            value["kind_by_level"] = dict(sorted(entry["kind_by_level"].items()))
        classes[code] = value
    return classes


def build() -> dict:
    backend = backend_domains()
    firmware = firmware_domains()
    domains = {}
    for name, description in DOMAINS.items():
        back = sorted(backend.get(name, set()))
        fw = sorted(firmware.get(name, set()) - set(back))
        domains[name] = {"description": description, "backend": back, "firmware": fw}
    result = {
        "generated_by": "scripts/export_word_codes.py",
        "note": "Generated file. Run `python scripts/export_word_codes.py` after adding a code, then add its words.",
        "domains": domains,
    }
    log = domains["log"]
    result["log_classes"] = _log_classes(set(log["backend"]) | set(log["firmware"]))
    return result


def render(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when a checked-in copy is stale")
    args = parser.parse_args(argv)
    data = build()
    text = render(data)
    stale = [path for path in OUTPUTS if not path.exists() or path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in stale:
            print(f"{_rel(path)} is out of date. Run `python scripts/export_word_codes.py` and add words "
                  "for the new codes.", file=sys.stderr)
        return 1 if stale else 0
    for path in OUTPUTS:
        path.write_text(text, encoding="utf-8")
    counts = {name: len(d["backend"]) + len(d["firmware"]) for name, d in data["domains"].items()}
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
