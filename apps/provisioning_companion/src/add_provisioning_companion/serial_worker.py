from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

MAC_PATTERN = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")

TOOLS_ROOT = Path(
    os.environ.get(
        "ADD_ZONE_LITE_TOOLS",
        str(Path(getattr(sys, "_MEIPASS", "")) / "firmware_tools")
        if getattr(sys, "frozen", False)
        else str(Path(__file__).resolve().parents[4] / "firmware" / "zone_lite" / "tools"),
    )
)
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


def _tool(name: str) -> str:
    configured = os.environ.get(f"ADD_{name.upper()}_PATH")
    suffix = ".exe" if os.name == "nt" else ""
    sidecar = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    resolved = configured or (str(sidecar) if sidecar.is_file() else None)
    resolved = resolved or shutil.which(name) or shutil.which(f"{name}.py")
    if not resolved:
        raise RuntimeError(f"Bundled {name} tool is unavailable")
    return resolved


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return completed.stdout


def _efuse_flag_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "enabled", "yes"}


def enumerate_candidates() -> list[dict[str, Any]]:
    from serial.tools import list_ports

    candidates = []
    esptool = _tool("esptool")
    for port in list_ports.comports():
        try:
            identity = _run(
                [esptool, "--chip", "esp32s3", "--port", port.device, "chip-id"]
            )
            match = MAC_PATTERN.search(identity)
            if not match or "ESP32-S3" not in identity.upper():
                continue
            flash = _run(
                [esptool, "--chip", "esp32s3", "--port", port.device, "flash-id"]
            )
            size_match = re.search(r"Detected flash size:\s*(\d+)MB", flash, re.IGNORECASE)
            flash_size = int(size_match.group(1)) * 1024 * 1024 if size_match else 0
            psram_match = re.search(r"Embedded PSRAM\s+(\d+)MB", identity, re.IGNORECASE)
            psram_size = int(psram_match.group(1)) * 1024 * 1024 if psram_match else 0
            candidates.append(
                {
                    "port": port.device,
                    "hardware_mac": match.group(1).lower(),
                    "chip": "ESP32-S3",
                    "flash_size_bytes": flash_size,
                    "psram_mode": "octal" if psram_size == 8 * 1024 * 1024 else "unknown",
                    "psram_size_bytes": psram_size,
                    "usb_identity": {
                        "vid": port.vid or 0,
                        "pid": port.pid or 0,
                        "serial_number": port.serial_number or "",
                        "manufacturer": port.manufacturer or "",
                    },
                }
            )
        except (subprocess.SubprocessError, RuntimeError):
            continue
    return candidates


def _efuse_evidence(port: str) -> dict[str, Any]:
    espefuse = _tool("espefuse")
    with tempfile.TemporaryDirectory(prefix="add-efuse-summary-") as directory:
        summary = Path(directory) / "summary.json"
        _run(
            [
                espefuse,
                "--chip",
                "esp32s3",
                "--port",
                port,
                "summary",
                "--format",
                "json",
                "--file",
                str(summary),
            ]
        )
        values = json.loads(summary.read_text(encoding="utf-8"))
    key0 = values.get("BLOCK_KEY0", {})
    purpose0 = values.get("KEY_PURPOSE_0", {}).get("value")
    secure_boot = _efuse_flag_enabled(
        values.get("SECURE_BOOT_EN", {}).get("value", 0)
    )
    digest_keys = []
    for index in range(6):
        purpose = str(values.get(f"KEY_PURPOSE_{index}", {}).get("value", ""))
        block = values.get(f"BLOCK_KEY{index}", {})
        digest = str(block.get("raw_value") or block.get("value") or "")
        if "SECURE_BOOT_DIGEST" in purpose and digest and digest.strip("0x0"):
            digest_keys.append(digest)
    digest_keys.sort()
    raw_key0 = str(key0.get("raw_value", ""))
    key0_empty = raw_key0 in {"", "0x" + ("0" * 64)} and bool(
        key0.get("writeable", False)
    )
    if key0_empty and purpose0 == "USER" and not secure_boot:
        classification = "BLANK"
    elif purpose0 == "HMAC_UP" and secure_boot and digest_keys:
        classification = "TRUSTED_SECURE"
    elif purpose0 == "HMAC_UP" and not secure_boot:
        classification = "LEGACY"
    else:
        classification = "UNKNOWN"
    return {
        "efuse_classification": classification,
        "secure_boot_digests": digest_keys,
        "hmac_key_purpose": purpose0 or "",
    }


def probe(selected_port: str | None = None) -> dict[str, Any]:
    candidates = enumerate_candidates()
    if not candidates:
        raise RuntimeError("NO_DEVICE")
    if selected_port:
        candidates = [item for item in candidates if item["port"] == selected_port]
        if not candidates:
            raise RuntimeError("SELECTED_DEVICE_DISCONNECTED")
    elif len(candidates) > 1:
        return {"selection_required": True, "candidates": candidates}
    selected = candidates[0]
    if selected["flash_size_bytes"] != 16 * 1024 * 1024:
        raise RuntimeError("WRONG_FLASH_SIZE")
    if selected["psram_mode"] != "octal" or selected["psram_size_bytes"] != 8 * 1024 * 1024:
        raise RuntimeError("WRONG_PSRAM_PROFILE")
    selected.update(_efuse_evidence(selected["port"]))
    return selected


def resolve_port(expected_mac: str) -> dict[str, str]:
    normalized = expected_mac.lower().replace("-", ":")
    matches = [
        item for item in enumerate_candidates() if item["hardware_mac"] == normalized
    ]
    if len(matches) != 1:
        raise RuntimeError("EXPECTED_DEVICE_NOT_CONNECTED")
    return {"port": str(matches[0]["port"])}


def _verify_factory_artifact(
    artifact: dict,
    *,
    public_key_pem_b64: str,
    expected_manifest_sha256: str,
) -> None:
    factory = artifact["factory_manifest"]
    canonical = json.dumps(factory, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != expected_manifest_sha256:
        raise RuntimeError("FACTORY_MANIFEST_HASH_MISMATCH")
    try:
        public_key = serialization.load_pem_public_key(
            base64.b64decode(public_key_pem_b64, validate=True)
        )
        public_key.verify(
            base64.b64decode(artifact["factory_manifest_signature"], validate=True),
            canonical,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise RuntimeError("FACTORY_MANIFEST_SIGNATURE_MISMATCH") from exc
    declared = {
        item["name"]: (int(item["offset"]), int(item["size"]), item["sha256"])
        for item in factory.get("images", [])
    }
    supplied = {
        item["name"]: (int(item["offset"]), int(item["size"]), item["sha256"])
        for item in artifact.get("images", [])
    }
    if declared != supplied:
        raise RuntimeError("FACTORY_IMAGE_INVENTORY_MISMATCH")


def _unpack_artifact(artifact: dict, directory: Path, private_key: bytes) -> tuple[Path, Path, Path]:
    from provisioning_envelope import decrypt_envelope

    manifest = artifact["provisioning_manifest"]
    target_mac = artifact["target_mac"]
    nvs_ciphertext = base64.b64decode(artifact["provisioning_ciphertext_b64"], validate=True)
    hmac_ciphertext = base64.b64decode(artifact["hmac_ciphertext_b64"], validate=True)
    if hashlib.sha256(nvs_ciphertext).hexdigest() != manifest["ciphertext_sha256"]:
        raise RuntimeError("PACKAGE_HASH_MISMATCH")
    if hashlib.sha256(hmac_ciphertext).hexdigest() != manifest["hmac_key"][
        "ciphertext_sha256"
    ]:
        raise RuntimeError("PACKAGE_HASH_MISMATCH")
    nvs = decrypt_envelope(nvs_ciphertext, private_key, manifest)
    hmac_key = decrypt_envelope(hmac_ciphertext, private_key, manifest["hmac_key"])
    if hashlib.sha256(nvs).hexdigest() != manifest["aad"]["nvs_sha256"]:
        raise RuntimeError("PACKAGE_HASH_MISMATCH")
    if len(hmac_key) != 32 or manifest["aad"]["target_mac"] != target_mac:
        raise RuntimeError("PACKAGE_MAC_MISMATCH")
    nvs_path = directory / "provision.bin"
    hmac_path = directory / "hmac-key.bin"
    nvs_path.write_bytes(nvs)
    hmac_path.write_bytes(hmac_key)
    nvs_path.chmod(0o600)
    hmac_path.chmod(0o600)
    bundle = directory / "bundle"
    bundle.mkdir()
    for image in artifact["images"]:
        content = base64.b64decode(image["content_b64"], validate=True)
        if hashlib.sha256(content).hexdigest() != image["sha256"]:
            raise RuntimeError("BUNDLE_HASH_MISMATCH")
        (bundle / image["name"]).write_bytes(content)
    factory = artifact["factory_manifest"]
    images = {item["name"]: item for item in artifact["images"]}
    compatibility = {
        "target_mac": target_mac,
        "git_sha": factory["git_sha"],
        "bootloader_sha256": images["bootloader-signed.bin"]["sha256"],
        "application_sha256": images["zone-lite-signed.bin"]["sha256"],
    }
    (bundle / "bootstrap-manifest.json").write_text(
        json.dumps(compatibility), encoding="utf-8"
    )
    provisioning_manifest = directory / "provisioning-manifest.json"
    provisioning_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, nvs_path, hmac_path


def flash(
    artifact_path: str,
    private_key_b64: str,
    expected_mac: str,
    expected_session_id: str,
    expected_classification: str,
    factory_signing_public_key_pem_b64: str,
    expected_factory_manifest_sha256: str,
    resume_after_efuse: bool,
    port: str,
    emit,
) -> dict[str, Any]:
    from flash_prepared_zone_lite import (
        flash_and_verify,
        flash_managed_and_verify,
        validate_inputs,
    )
    from provision_zone_lite import ensure_nvs_hmac_key, normalize_mac, read_mac

    if read_mac(_tool("esptool"), port) != normalize_mac(expected_mac):
        raise RuntimeError("TARGET_MAC_MISMATCH")
    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    if normalize_mac(artifact["target_mac"]) != normalize_mac(expected_mac):
        raise RuntimeError("PACKAGE_MAC_MISMATCH")
    if artifact.get("session_id") != expected_session_id:
        raise RuntimeError("PACKAGE_SESSION_MISMATCH")
    if artifact.get("hardware_classification") != expected_classification:
        raise RuntimeError("PACKAGE_CLASSIFICATION_MISMATCH")
    _verify_factory_artifact(
        artifact,
        public_key_pem_b64=factory_signing_public_key_pem_b64,
        expected_manifest_sha256=expected_factory_manifest_sha256,
    )
    with tempfile.TemporaryDirectory(prefix="add-flash-") as directory:
        workspace = Path(directory)
        bundle, nvs, hmac_key = _unpack_artifact(
            artifact, workspace, base64.b64decode(private_key_b64)
        )
        manifest = workspace / "provisioning-manifest.json"
        validate_inputs(
            signed_package=bundle,
            provisioning_manifest=manifest,
            provision_nvs=nvs,
            hmac_key=hmac_key,
            mac=normalize_mac(expected_mac),
            expected_git_sha=artifact["factory_manifest"]["git_sha"],
        )
        emit("PACKAGE_READY", 20, {})
        if artifact["hardware_classification"] in {"BLANK_NEW", "KNOWN_LEGACY"}:
            if resume_after_efuse:
                if _efuse_evidence(port).get("hmac_key_purpose") != "HMAC_UP":
                    raise RuntimeError("EFUSE_RESUME_VERIFICATION_FAILED")
            else:
                emit("EFUSE_BURNING", 24, {})
                ensure_nvs_hmac_key(
                    espefuse=_tool("espefuse"),
                    port=port,
                    mac=normalize_mac(expected_mac),
                    key_path=hmac_key,
                    summary_path=workspace / "efuse-after.json",
                    confirmation=normalize_mac(expected_mac),
                    trust_existing=artifact["hardware_classification"] == "KNOWN_LEGACY",
                    split_root_recovery=False,
                )
            emit("EFUSE_VERIFIED", 30, {})
        emit("FLASHING", 35, {})
        flash_function = (
            flash_managed_and_verify
            if artifact["hardware_classification"] == "KNOWN_SECURE_MANAGED"
            else flash_and_verify
        )

        def flash_progress(state: str, completed_bytes: int, total_bytes: int) -> None:
            transfer_fraction = completed_bytes / max(total_bytes, 1)
            emit(
                state,
                35 + round(43 * transfer_fraction),
                {
                    "bytes_completed": completed_bytes,
                    "bytes_total": total_bytes,
                    "transfer_phase": state,
                },
            )

        flash_function(
            esptool=_tool("esptool"),
            port=port,
            signed_package=bundle,
            provision_nvs=nvs,
            progress=flash_progress,
        )
    emit("LOCAL_VERIFIED", 86, {"hardware_mac": expected_mac})
    emit("BOOT_VERIFYING", 88, {"hardware_mac": expected_mac})
    _run([_tool("esptool"), "--chip", "esp32s3", "--port", port, "run"])
    from serial import Serial

    transcript = bytearray()
    deadline = time.monotonic() + 30
    with Serial(port, 115200, timeout=0.25) as serial_port:
        while time.monotonic() < deadline and len(transcript) < 64 * 1024:
            transcript.extend(serial_port.read(1024))
            text = transcript.decode("utf-8", errors="ignore")
            if "Zone Lite" in text and (
                "ADD automatic onboarding completed" in text
                or "ADD connector" in text
                or "onboarding" in text.lower()
            ):
                break
    text = transcript.decode("utf-8", errors="ignore")
    if "Zone Lite" not in text:
        raise RuntimeError("BOOT_DESCRIPTOR_NOT_OBSERVED")
    post_boot_evidence = _efuse_evidence(port)
    if (
        post_boot_evidence["efuse_classification"] != "TRUSTED_SECURE"
        or not post_boot_evidence["secure_boot_digests"]
    ):
        raise RuntimeError("POST_BOOT_SECURITY_VERIFICATION_FAILED")
    emit(
        "WAITING_FOR_ONBOARDING",
        90,
        {
            "hardware_mac": expected_mac,
            "boot_transcript_sha256": hashlib.sha256(bytes(transcript)).hexdigest(),
            "descriptor_observed": True,
            **post_boot_evidence,
        },
    )
    return {"hardware_mac": expected_mac, "locally_verified": True}


def worker_main(connection: Connection) -> None:
    try:
        request = connection.recv()
        if request["operation"] == "probe":
            connection.send({"ok": True, "result": probe(request.get("port"))})
        elif request["operation"] == "resolve_port":
            connection.send(
                {"ok": True, "result": resolve_port(request["expected_mac"])}
            )
        elif request["operation"] == "flash":
            def emit(state: str, progress: int, details: dict) -> None:
                connection.send(
                    {
                        "event": True,
                        "state": state,
                        "progress": progress,
                        "details": details,
                    }
                )

            result = flash(
                request["artifact_path"],
                request["private_key_b64"],
                request["expected_mac"],
                request["expected_session_id"],
                request["expected_classification"],
                request["factory_signing_public_key_pem_b64"],
                request["expected_factory_manifest_sha256"],
                bool(request.get("resume_after_efuse", False)),
                request["port"],
                emit,
            )
            connection.send({"ok": True, "result": result})
        else:
            raise ValueError("Unsupported USB worker operation")
    except Exception as exc:
        connection.send({"ok": False, "error": str(exc)[:160]})
    finally:
        connection.close()
