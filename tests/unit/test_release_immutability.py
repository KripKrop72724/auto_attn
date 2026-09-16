"""A correctly signed replacement still cannot reuse an existing release identity."""
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.models import Base
from zk_add.ota import sync_release_store
from zk_add.settings import settings


@pytest.mark.parametrize("field,value", [
    ("version", "2.5.1"), ("git_sha", "d" * 40), ("application_sha256", "d" * 64),
    ("signing_key_id", "different"), ("minimum_bootstrap_version", "2.4.12"),
    ("created_at", "changed"), ("image", b"different signed application"),
])
def test_signed_release_identity_cannot_change(tmp_path, monkeypatch, field, value):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", base64.b64encode(public).decode())
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    release = tmp_path / "2.5.2"
    release.mkdir()
    image = release / "firmware.bin"
    image.write_bytes(b"signed application fixture")
    manifest = {"release_id": "zone-lite-2.5.2", "version": "2.5.2", "git_sha": "a" * 40,
                "application_sha256": "c" * 64, "image_name": image.name,
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "image_size": image.stat().st_size,
                "partition_layout": "zone-lite-ota-v1", "minimum_bootstrap_version": "2.2.0",
                "signing_key_id": "key", "created_at": "original", "schema_version": 2}

    def publish():
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (release / "manifest.json").write_bytes(canonical)
        signature = key.sign(canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        (release / "manifest.sig").write_text(base64.b64encode(signature).decode())

    publish()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_release_store(session)
        session.commit()
        sync_release_store(session)  # Identical packages remain idempotent.
        if field == "image":
            image.write_bytes(value)
            manifest["image_sha256"] = hashlib.sha256(value).hexdigest()
            manifest["image_size"] = len(value)
        else:
            manifest[field] = value
        publish()
        with pytest.raises(RuntimeError, match="changed"):
            sync_release_store(session)
    engine.dispose()
