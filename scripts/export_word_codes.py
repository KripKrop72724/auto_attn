#!/usr/bin/env python3
"""Export every code the dashboard may show, by domain, for the words guard.

Reads the firmware C sources and the backend Python sources as text (regex
and AST); nothing is imported or run. The result is checked in twice:

* apps/add_frontend/src/words/codes.generated.json (the frontend words test)
* apps/add_backend/zk_add/word_codes.json (the backend copy)

tests/unit/test_word_codes.py fails when either file is out of date or when a
log code has no plain words. Run ``python scripts/export_word_codes.py`` after
adding a code, then add its words.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware/zone_lite/main"
BACKEND = ROOT / "apps/add_backend/zk_add"
OUTPUTS = (ROOT / "apps/add_frontend/src/words/codes.generated.json", BACKEND / "word_codes.json")
LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}
CODE = re.compile(r'"([A-Z][A-Z0-9_]{3,})"')
LOG_HELPERS = re.compile(r"\b(add_connector_log_acknowledged|add_connector_log|logf_line|say|queue_line)\s*\(")
# Codes the backend writes into device logs itself.
BACKEND_LOG_CODES = ("DEVICE_MESSAGE_REJECTED", "ADD_MESSAGE_PROCESSING_FAILED", "SOURCE_TAIL_COVERAGE_INACTIVE",
                     "SOURCE_TAIL_EPOCH_STALE")
# Codes only old link box software sends.
LEGACY_CODES = {"log": ["BLOCKED_IDENTITY_RECOVERY_DEFERRED"]}


def _call_args(text: str, start: int) -> str:
    depth, index = 1, start
    while index < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        index += 1
    return text[start:index - 1]


def firmware_log_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted(FIRMWARE.glob("*.c")):
        text = path.read_text(errors="replace")
        for match in LOG_HELPERS.finditer(text):
            codes.update(CODE.findall(_call_args(text, match.end())))
        for array in re.finditer(r"codes\[\]\s*=\s*\{([^}]*)\}", text):
            codes.update(CODE.findall(array.group(1)))
    return codes - LEVELS


def _tree(name: str) -> ast.Module:
    return ast.parse((BACKEND / name).read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
            node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets) and node.value is not None:
            return node.value
    return None


def _strings(node) -> set[str]:
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)} \
        if node is not None else set()


def _keys(node) -> set[str]:
    if isinstance(node, ast.Dict):
        return {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    return set()


def _codes(values: set[str]) -> set[str]:
    return {value for value in values if re.fullmatch(r"[A-Z][A-Z0-9_]+", value)}


def _attribute_assignments(attribute: str, files: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(
                        target, ast.Name) else None
                    if name == attribute and isinstance(node.value, ast.Constant) and isinstance(
                            node.value.value, str):
                        found.add(node.value.value)
            if isinstance(node, ast.keyword) and node.arg == attribute and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                found.add(node.value.value)
    return _codes(found)


def _command_status_literal() -> set[str]:
    for node in ast.walk(_tree("schemas.py")):
        if isinstance(node, ast.ClassDef) and node.name == "CommandUpdate":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "status":
                    return _codes(_strings(item.annotation))
    return set()


def _log_classes(codes: set[str]) -> dict[str, dict]:
    data = ast.literal_eval(_assigned(ast.parse((BACKEND / "activity_words_data.py").read_text()), "WORDS"))
    classes = {}
    for code in sorted(codes):
        entry = data.get(code)
        if entry is None:
            continue
        value = {"group": entry["group"], "kind": entry["kind"], "act": entry["act"]}
        if entry.get("kind_by_level"):
            value["kind_by_level"] = dict(sorted(entry["kind_by_level"].items()))
        classes[code] = value
    return classes


def build() -> dict:
    backend_files = sorted(BACKEND.glob("*.py"))
    log = firmware_log_codes() | set(BACKEND_LOG_CODES) | set(LEGACY_CODES["log"])
    ota = _tree("ota.py")
    service = _tree("service.py")
    domains = {
        "log": log,
        "problem": _keys(_assigned(_tree("device_health.py"), "POLICY")),
        "ords_status": _codes(_strings(_tree("ords_states.py"))) | _attribute_assignments("ords_status", backend_files),
        "identity_status": _codes(_strings(_tree("identity_states.py")))
        | _attribute_assignments("identity_resolution_status", backend_files),
        "command_status": _command_status_literal() | _codes(_strings(_assigned(service, "ACTIVE_COMMAND_STATES"))),
        "command_type": _attribute_assignments("command_type", backend_files) | {"ESP_REBOOT"},
        "deployment_state": _codes(_strings(_assigned(ota, "DEPLOYMENT_TRANSITIONS")))
        | _codes(_strings(_assigned(ota, "TERMINAL_DEPLOYMENT_STATES"))),
        "campaign_state": _codes(_strings(_assigned(_tree("firmware_campaigns.py"), "LIVE_STATES")))
        | _codes(_strings(_assigned(_tree("firmware_campaigns.py"), "CLOSED_STATES"))),
        "bridge_event": _codes(_strings(_assigned(_tree("bridge_observation.py"), "EVENTS"))),
        "reconciliation_status": _attribute_assignments("status", [BACKEND / "reconciliation.py"])
        | _attribute_assignments("phase", [BACKEND / "reconciliation.py"]),
        "force_release_message": _keys(_assigned(_tree("attendance_force_release.py"), "MESSAGES")),
        "direct_ords_reason": _keys(_assigned(_tree("attendance_direct_ords.py"), "REASONS")),
        "rejection": _codes(_strings(_assigned(service, "REJECTION_CODES"))),
        "ota_failure": _keys(_assigned(service, "OTA_FAILURE_MESSAGES"))
        | _codes(_strings(_assigned(service, "LOCAL_BOOT_WAIT_REASONS"))),
        "public_status_reason": _keys(_assigned(_tree("public_status.py"), "REASONS")),
    }
    return {
        "_comment": "Generated by scripts/export_word_codes.py. Do not edit by hand.",
        "domains": {name: sorted(values) for name, values in sorted(domains.items())},
        "log_classes": _log_classes(log),
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=False, ensure_ascii=False) + "\n"


def main() -> int:
    text = render(build())
    for path in OUTPUTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"Wrote {len(OUTPUTS)} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
