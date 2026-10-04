"""Create/remove only a labelled, isolated Oracle database containing test data."""
import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

IMAGE = "container-registry.oracle.com/database/free@sha256:cf540c3fa190d7cffad08c491652ac07fc70314e510f6b87890449517d565e94"
LABEL = "com.zkt.purpose=synthetic-qualification"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"zkt-[a-z0-9-]{1,55}", args.container):
        raise SystemExit("Use a dedicated zkt- test-container name")
    network = args.container + "-network"
    if args.action == "stop":
        found = subprocess.run(["docker", "inspect", args.container], capture_output=True, text=True)
        if found.returncode == 0:
            info = json.loads(found.stdout)[0]
            if info["Config"].get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification":
                raise SystemExit("Refusing to remove an unlabelled container")
            docker("rm", "-fv", args.container)
        found = subprocess.run(["docker", "network", "inspect", network], capture_output=True, text=True)
        if found.returncode == 0:
            info = json.loads(found.stdout)[0]
            if not info["Internal"] or info.get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification":
                raise SystemExit("Refusing to remove an unlabelled network")
            docker("network", "rm", network)
        return
    # Existing names are an error, never permission to replace another database.
    docker("network", "create", "--internal", "--label", LABEL, network)
    with tempfile.TemporaryDirectory(prefix="zkt-synthetic-") as folder:
        config = Path(folder) / "oracle.env"
        config.write_text("ORACLE_PWD=T" + secrets.token_hex(18) + "a9\nORACLE_CHARACTERSET=AL32UTF8\n")
        config.chmod(0o600)
        docker("run", "-d", "--name", args.container, "--label", LABEL, "--network", network,
               "--memory", "3g", "--cpus", "2", "--shm-size", "1g", "--env-file", str(config), IMAGE)
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        info = json.loads(docker("inspect", args.container))[0]
        if info["State"].get("Health", {}).get("Status") == "healthy":
            print("Synthetic Oracle healthy; internal-only network, no published ports.")
            return
        if info["State"]["Status"] != "running":
            break
        time.sleep(5)
    raise SystemExit("Synthetic Oracle did not become healthy within eight minutes")


if __name__ == "__main__":
    main()
