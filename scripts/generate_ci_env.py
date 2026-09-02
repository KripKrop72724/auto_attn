#!/usr/bin/env python3
"""Create a disposable, randomly keyed ADD environment for CI container smoke tests."""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from cryptography.fernet import Fernet


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    password_hash = PasswordHasher().hash(secrets.token_urlsafe(32))
    values = {
        "ADD_POSTGRES_DB": "attendance_devices",
        "ADD_POSTGRES_USER": "add_service",
        "ADD_POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "ADD_POSTGRES_MEMORY_LIMIT": "1g",
        "ADD_POSTGRES_SHARED_BUFFERS": "256MB",
        "ADD_POSTGRES_EFFECTIVE_CACHE_SIZE": "768MB",
        "ADD_POSTGRES_MAINTENANCE_WORK_MEM": "128MB",
        "ADD_POSTGRES_SHM_SIZE": "256mb",
        "ADD_REDIS_MEMORY_LIMIT": "384m",
        "ADD_REDIS_MAXMEMORY": "256mb",
        "ADD_API_MEMORY_LIMIT": "1g",
        "ADD_PROVISIONER_MEMORY_LIMIT": "512m",
        "ADD_WEB_MEMORY_LIMIT": "256m",
        "ADD_WATCHDOG_MEMORY_LIMIT": "128m",
        "ADD_ADMIN_USERNAME": "StateHealthAdmin",
        "ADD_ADMIN_PASSWORD_HASH": password_hash,
        "ADD_ADMIN_COOKIE_SECURE": "false",
        "ADD_PII_FERNET_KEY": Fernet.generate_key().decode(),
        "ADD_PII_LOOKUP_KEY": secrets.token_hex(32),
        "ADD_FLEET_ROOT_SECRET": secrets.token_urlsafe(64),
        "ADD_PUBLIC_DEVICE_WS_URL": "wss://autoattn.slichealth.com/device/v2/stream",
        "ADD_ORDS_BASE_URL": "https://example.invalid/ords/ci/raw_attn_capture_event",
        "ADD_ORDS_USERNAME": "ci_service",
        "ADD_ORDS_PASSWORD": secrets.token_urlsafe(48),
    }
    # Compose interpolates dollar signs in unquoted env-file values. Single quotes
    # preserve Argon2's dollar-delimited verifier exactly.
    if any("'" in value for value in values.values()):
        raise ValueError("Generated values unexpectedly contain a single quote")
    args.output.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    args.output.chmod(0o600)
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
