from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import BaseModel, ConfigDict, Field

from build_provisioning_package import main as _cli_main  # noqa: F401
from build_provisioning_package import validate_request

ARTIFACT_ROOT = Path(os.environ.get("ADD_PROVISIONING_ARTIFACT_PATH", "/artifacts"))
FACTORY_ROOT = Path(os.environ.get("ADD_PROVISIONING_FACTORY_STORE_PATH", "/factory-firmware"))
IDF_PATH = Path(os.environ.get("IDF_PATH", "/opt/esp/idf"))
ARTIFACT_SECONDS = int(os.environ.get("ADD_PROVISIONING_ARTIFACT_SECONDS", "900"))


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=8, max_length=96)
    hardware_mac: str
    hardware_classification: str
    recipient_public_key: str
    bundle_id: str
    bundle_storage_prefix: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    bundle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: dict
    managed_defaults: dict


def _authorize(value: str | None) -> None:
    expected = os.environ.get("ADD_PROVISIONING_INTERNAL_TOKEN")
    supplied = (value or "").removeprefix("Bearer ")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Internal authentication required.")


def _cleanup_expired() -> None:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc).timestamp() - ARTIFACT_SECONDS
    for path in ARTIFACT_ROOT.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass


async def _artifact_reaper() -> None:
    while True:
        _cleanup_expired()
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_artifact_reaper())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="ADD Protected Provisioner",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def sanitized_validation_error(_request, error: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": item.get("type", "value_error"),
                    "loc": list(item.get("loc", ())),
                    "msg": item.get("msg", "Invalid value"),
                }
                for item in error.errors()
            ]
        },
    )


def _factory_bundle(body: PackageRequest) -> tuple[dict, Path]:
    directory = (FACTORY_ROOT.resolve() / body.bundle_storage_prefix).resolve()
    if FACTORY_ROOT.resolve() not in directory.parents:
        raise ValueError("Invalid factory bundle path")
    manifest_path = directory / "manifest.json"
    signature_path = directory / "manifest.sig"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if not hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), body.bundle_manifest_sha256):
        raise ValueError("Factory manifest digest mismatch")
    if manifest.get("bundle_id") != body.bundle_id:
        raise ValueError("Factory bundle identity mismatch")
    if manifest.get("setup_password_supplied") is not True:
        raise ValueError("Protected setup-password evidence is missing")
    if not signature_path.is_file():
        raise ValueError("Factory manifest signature is missing")
    public_key_b64 = os.environ.get("ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64")
    if not public_key_b64:
        raise ValueError("Factory verification key is unavailable")
    public_key = serialization.load_pem_public_key(
        base64.b64decode(public_key_b64, validate=True)
    )
    public_key.verify(
        base64.b64decode(signature_path.read_text(encoding="ascii").strip(), validate=True),
        canonical,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    for image in manifest.get("images", []):
        name = Path(str(image.get("name", ""))).name
        path = directory / name
        if not name or not path.is_file() or path.stat().st_size != int(image.get("size", -1)):
            raise ValueError("Factory image inventory is incomplete")
        if not hmac.compare_digest(
            hashlib.sha256(path.read_bytes()).hexdigest(), str(image.get("sha256", ""))
        ):
            raise ValueError("Factory image integrity failed")
    return manifest, directory


def _build_request(body: PackageRequest) -> dict:
    config = body.configuration
    values = {
        "request_id": body.session_id,
        "target_mac": body.hardware_mac,
        "recipient_public_key_b64": body.recipient_public_key,
        "wifi_ssid": config.get("wifi_ssid"),
        "wifi_password": config.get("wifi_password"),
        "zkt_comm_key": config.get("communication_key"),
        "zkt_port": config.get("zkt_port", 4370),
        "zkt_preferred_ip": config.get("preferred_ip", "0.0.0.0"),
        "zkt_expected_serial": "",
        "zone_device_id": config.get("device_id"),
        "zone_id": config.get("zone_id"),
        "zone_name": config.get("zone_name"),
        "zkt_recovery_enabled": False,
    }
    return validate_request(values)


def _run_builder(request: dict, output: Path) -> None:
    import subprocess
    import sys

    request_path = output.parent / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "/app/firmware/zone_lite/tools/build_provisioning_package.py",
            "--request",
            str(request_path),
            "--output",
            str(output),
            "--idf-path",
            str(IDF_PATH),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "/app/firmware/zone_lite/tools",
            "ADD_FLEET_ROOT_SECRET": os.environ["ADD_FLEET_ROOT_SECRET"],
            "ADD_ORDS_BASE_URL": os.environ["ADD_ORDS_BASE_URL"],
            "ADD_ORDS_USERNAME": os.environ["ADD_ORDS_USERNAME"],
            "ADD_ORDS_PASSWORD": os.environ["ADD_ORDS_PASSWORD"],
        },
    )


@app.get("/health/live")
def live():
    """Prove the protected worker process can serve requests."""

    return {"ok": True, "service": "add-provisioner"}


@app.get("/health/ready")
def ready():
    required = (
        "ADD_FLEET_ROOT_SECRET",
        "ADD_ORDS_BASE_URL",
        "ADD_ORDS_USERNAME",
        "ADD_ORDS_PASSWORD",
        "ADD_PROVISIONING_INTERNAL_TOKEN",
        "ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing or not IDF_PATH.is_dir() or not FACTORY_ROOT.is_dir():
        raise HTTPException(status_code=503, detail="Protected provisioner is not ready.")
    return {"ok": True}


@app.post("/internal/v1/packages", status_code=201)
def create_package(
    body: PackageRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    _cleanup_expired()
    try:
        factory_manifest, factory_directory = _factory_bundle(body)
        request = _build_request(body)
        with tempfile.TemporaryDirectory(prefix="add-provisioning-", dir="/tmp") as temporary:
            workspace = Path(temporary)
            package_dir = workspace / "package"
            package_dir.mkdir(mode=0o700)
            _run_builder(request, package_dir)
            provisioning_manifest = json.loads(
                (package_dir / "manifest.json").read_text(encoding="utf-8")
            )
            images = []
            for image in factory_manifest.get("images", []):
                name = Path(str(image["name"])).name
                path = factory_directory / name
                images.append(
                    {
                        "name": name,
                        "offset": int(image["offset"]),
                        "size": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "content_b64": base64.b64encode(path.read_bytes()).decode(),
                    }
                )
            artifact = {
                "schema_version": 1,
                "session_id": body.session_id,
                "target_mac": body.hardware_mac,
                "hardware_classification": body.hardware_classification,
                "factory_manifest": factory_manifest,
                "factory_manifest_signature": (
                    factory_directory / "manifest.sig"
                ).read_text(encoding="ascii").strip(),
                "images": images,
                "provisioning_manifest": provisioning_manifest,
                "provisioning_ciphertext_b64": base64.b64encode(
                    (package_dir / "provision.bin.enc").read_bytes()
                ).decode(),
                "hmac_ciphertext_b64": base64.b64encode(
                    (package_dir / "hmac-key.bin.enc").read_bytes()
                ).decode(),
            }
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="Protected provisioning package could not be generated.",
        ) from exc
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ARTIFACT_SECONDS)
    artifact["expires_at"] = expires_at.isoformat()
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    artifact_id = str(uuid4())
    final_path = ARTIFACT_ROOT / f"{artifact_id}.json"
    temporary_path = ARTIFACT_ROOT / f".{artifact_id}.tmp"
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "artifact_id": artifact_id,
        "artifact_sha256": hashlib.sha256(encoded).hexdigest(),
        "expires_at": expires_at.isoformat(),
        "manifest": {
            "schema_version": 1,
            "target_mac": body.hardware_mac,
            "bundle_id": body.bundle_id,
            "provisioning_ciphertext_sha256": provisioning_manifest["ciphertext_sha256"],
            "hmac_ciphertext_sha256": provisioning_manifest["hmac_key"][
                "ciphertext_sha256"
            ],
        },
    }
