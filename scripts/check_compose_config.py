#!/usr/bin/env python3
"""Fail unless the rendered ADD Compose model exposes only the intended ports."""

from __future__ import annotations

import json
import sys


def main() -> int:
    services = json.load(sys.stdin)["services"]
    expected = {
        "add-web": ("0.0.0.0", "8095", 80),
        "add-api": ("0.0.0.0", "8096", 8096),
    }
    for service_name, binding in expected.items():
        actual = {
            (port.get("host_ip"), str(port.get("published")), port.get("target"))
            for port in services[service_name].get("ports", [])
        }
        if binding not in actual:
            raise SystemExit(f"{service_name} is not bound as {binding}: {sorted(actual)}")

    unexpectedly_public = {
        service_name
        for service_name in ("postgres", "redis", "add-provisioner")
        if services[service_name].get("ports")
    }
    if unexpectedly_public:
        raise SystemExit(f"Private services expose host ports: {sorted(unexpectedly_public)}")
    provisioner = services["add-provisioner"]
    if "add-private" in provisioner.get("networks", {}):
        raise SystemExit("The provisioner must use only its isolated internal network.")
    mounts = provisioner.get("volumes", [])
    if any(str(mount.get("source", "")).endswith("docker.sock") for mount in mounts):
        raise SystemExit("The provisioner must never receive the Docker socket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
