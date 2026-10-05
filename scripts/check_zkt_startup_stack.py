"""Cross-compile real startup adapters and bound their nested stack frames.

Run inside the pinned IDF environment after a bridge/writer build. This catches
the concrete supervisor overflow missed by host tests with large host stacks.
The explicit reserve covers callees outside these measured adapter frames;
this is not a field high-watermark or physical qualification measurement.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


def check(build: Path) -> dict:
    records = json.loads((build / "compile_commands.json").read_text())
    files = {"add_connector.c", "zkt_catalog_client.c"}
    frames = {}
    with tempfile.TemporaryDirectory(prefix="zkt-startup-stack-") as directory:
        output = Path(directory)
        found = set()
        for row in records:
            name = Path(row["file"]).name
            if name not in files:
                continue
            found.add(name)
            command = row.get("arguments") or shlex.split(row["command"])
            command = list(command)
            command[command.index("-o") + 1] = str(output / (name + ".o"))
            command.append("-fstack-usage")
            result = subprocess.run(command, cwd=row["directory"], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"Startup stack compilation failed for {name}")
        if found != files:
            raise ValueError("Missing production startup compilation commands")
        for path in output.glob("*.su"):
            for line in path.read_text().splitlines():
                location, size, kind = line.split("\t")
                name = re.split(r"[.$]", location.rsplit(":", 1)[-1])[0]
                if kind != "static":
                    raise ValueError(f"Unbounded startup compilation frame: {name}")
                frames[name] = max(frames.get(name, 0), int(size))
    chains = {
        "command_recovery": ("delivery_supervisor_task", "restore_command_inbox",
            "command_owner_recover", "zc_client_call", "zc_client_drain"),
        "retained_reply": ("delivery_supervisor_task", "restore_command_inbox", "zc_client_drain"),
        "catalog_recovery": ("delivery_supervisor_task", "catalog_owner_maintenance",
            "catalog_owner_recover", "zc_client_call", "zc_client_drain"),
    }
    source = Path(next(row["file"] for row in records if Path(row["file"]).name == "add_connector.c")).read_text()
    match = re.search(r'xTaskCreate\(delivery_supervisor_task,\s*"add_supervisor",\s*(\d+)', source)
    if match is None:
        raise ValueError("Supervisor stack allocation is not explicit")
    allocation, reserve = int(match[1]), 1024
    measured = {}
    for label, names in chains.items():
        if any(name not in frames for name in names):
            raise ValueError(f"Missing compiler stack evidence for {label}")
        measured[label] = sum(frames[name] for name in names)
        if measured[label] + reserve > allocation:
            raise ValueError(f"{label}: measured frames {measured[label]} + reserve {reserve} exceed {allocation}")
    return {"schema_version": 1, "allocation_bytes": allocation,
            "unmeasured_callee_reserve_bytes": reserve, "measured_chain_bytes": measured,
            "physical_stack_watermark": "NOT_PERFORMED"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(check(arguments.build.resolve()), sort_keys=True))
