"""A signed future package must not reach a backend that cannot admit it."""

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from zk_add import storage_contract
from zk_add.settings import settings
from zk_add.zkt_bridge_contract import bridge_contract, signed_hil_targets

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_deployed_firmware_contract.py"
spec = importlib.util.spec_from_file_location("deployed_firmware_contract", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def package(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", public)
    manifest = dict(
        version="2.6.16",
        release_id="zone-lite-2.6.16",
        firmware_family="zkt",
        project_name="zone_lite",
        release_channel="EXPERIMENTAL_HIL_ONLY",
        minimum_bootstrap_version="2.4.12",
        hil_targets=signed_hil_targets(),
        queue_storage=bridge_contract(),
    )

    def write(value=manifest):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        signature = key.sign(
            raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
        )
        (tmp_path / "manifest.json").write_bytes(raw)
        (tmp_path / "manifest.sig").write_bytes(base64.b64encode(signature))
        return [
            tmp_path / "manifest.json",
            tmp_path / "manifest.sig",
            hashlib.sha256(raw).hexdigest(),
        ]

    return manifest, write, public


def test_actual_installed_signature_and_contract_with_cli(package):
    _, write, public = package
    args = write()
    expected = "ADD_FIRMWARE_CONTRACT_ACCEPTED:" + args[2]
    assert checker.check(*args) == expected
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        env={**os.environ, "ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64": public},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0 and result.stdout.strip() == expected and result.stderr == ""


@pytest.mark.parametrize(
    "fault",
    [
        "changed-bytes",
        "bad-signature",
        "wrong-trust-root",
        "oversize-manifest",
        "oversize-signature",
        "malformed",
        "array",
        "writer",
        "wrong-family",
        "missing-contract",
        "bad-identity",
    ],
)
def test_refusal_has_no_arbitrary_package_or_configuration_output(
    package, monkeypatch, capsys, fault
):
    manifest, write, _ = package
    if fault == "writer":
        manifest["version"] = "2.7.0"
    elif fault == "wrong-family":
        manifest["firmware_family"] = "unsupported"
    elif fault == "missing-contract":
        del manifest["queue_storage"]
    args = write([] if fault == "array" else manifest)
    if fault in {"changed-bytes", "oversize-manifest", "malformed"}:
        args[0].write_bytes(
            b"sensitive-package-text" * 4000 if fault == "oversize-manifest" else b"not-json"
        )
        if fault != "changed-bytes":
            args[2] = hashlib.sha256(args[0].read_bytes()).hexdigest()
    if fault in {"bad-signature", "oversize-signature"}:
        args[1].write_bytes(b"sensitive-signature" * (1000 if fault == "oversize-signature" else 1))
    if fault == "wrong-trust-root":
        monkeypatch.setattr(
            settings, "firmware_signing_public_key_pem_b64", "unavailable-config-value"
        )
    if fault == "bad-identity":
        args[2] = "BAD"
    assert checker.main(list(map(str, args))) == 1
    assert capsys.readouterr() == ("ADD_FIRMWARE_CONTRACT_REJECTED\n", "")


def test_uses_the_deployed_validator_and_refuses_old_backend(package, monkeypatch, capsys):
    _, write, _ = package
    args = write()
    checked = []

    def old_validator(manifest, version):
        checked.append(version)
        raise ValueError("older installed backend has no bridge support")

    monkeypatch.setattr(storage_contract, "validate_storage_contract", old_validator)
    assert checker.main(list(map(str, args))) == 1
    assert checked == ["2.6.16"]
    assert capsys.readouterr().out == "ADD_FIRMWARE_CONTRACT_REJECTED\n"


def published_marker(manifest, count=1):
    return {
        "schema_version": 2,
        **{
            key: manifest[key]
            for key in ("version", "git_sha", "image_sha256", "application_sha256")
        },
        "targets": signed_hil_targets()[:count],
    }


def imported_manifest(manifest, count=1):
    return {
        **manifest,
        "_publication_mode": "HIL_ONLY",
        "_hil_target_mac": "",
        "_hil_targets": signed_hil_targets()[:count],
    }


@pytest.fixture
def published_release_database(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from zk_add import db
    from zk_add.ota import FirmwareRelease

    engine = create_engine("sqlite+pysqlite:///:memory:")
    FirmwareRelease.__table__.create(engine)
    maker = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    yield maker, FirmwareRelease
    engine.dispose()


def test_published_hil_is_existing_exact_unrevoked_and_read_only(
    package, published_release_database
):
    from datetime import datetime, timezone
    from sqlalchemy import event, select

    manifest, write, _ = package
    manifest.update(
        git_sha="a" * 40,
        image_sha256="b" * 64,
        image_size=123,
        signing_key_id="test-key",
        partition_layout="test",
        application_sha256="e" * 64,
    )
    paths = write()
    maker, Release = published_release_database
    signature = paths[1].read_text().strip()
    with maker() as session:
        row = Release(
            release_id=manifest["release_id"],
            version=manifest["version"],
            git_sha=manifest["git_sha"],
            image_sha256=manifest["image_sha256"],
            image_size=123,
            signing_key_id="test-key",
            partition_layout="test",
            storage_name="2.6.16",
            manifest=imported_manifest(manifest),
            manifest_signature=signature,
            state="HIL_ONLY",
        )
        session.add(row)
        session.commit()
    writes = []

    @event.listens_for(maker.kw["bind"], "before_cursor_execute")
    def sql(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("SELECT"):
            writes.append(statement)

    checker.require_published_hil(manifest, signature, published_marker(manifest))
    assert not writes
    event.remove(maker.kw["bind"], "before_cursor_execute", sql)
    for state, revoked in [
        ("REVOKED", None),
        ("HIL_ONLY", datetime.now(timezone.utc)),
        ("AVAILABLE", None),
    ]:
        with maker() as session:
            row = session.scalar(select(Release))
            row.state = state
            row.revoked_at = revoked
            session.commit()
        with pytest.raises(ValueError):
            checker.require_published_hil(manifest, signature, published_marker(manifest))
        with maker() as session:
            row = session.scalar(select(Release))
            assert row.state == state and (row.revoked_at is not None) == (revoked is not None)


@pytest.mark.parametrize(
    "fault", ["missing", "manifest", "signature", "source", "image", "size", "key"]
)
def test_published_hil_missing_or_changed_row_is_never_registered_or_repaired(
    package, published_release_database, fault
):
    from sqlalchemy import select, func

    manifest, write, _ = package
    manifest.update(
        git_sha="a" * 40,
        image_sha256="b" * 64,
        image_size=123,
        signing_key_id="key",
        application_sha256="e" * 64,
    )
    paths = write()
    signature = paths[1].read_text().strip()
    maker, Release = published_release_database
    if fault != "missing":
        row = Release(
            release_id=manifest["release_id"],
            version=manifest["version"],
            git_sha=manifest["git_sha"],
            image_sha256=manifest["image_sha256"],
            image_size=123,
            signing_key_id="key",
            partition_layout="test",
            storage_name="test",
            manifest=imported_manifest(manifest),
            manifest_signature=signature,
            state="HIL_ONLY",
        )
        if fault == "manifest":
            row.manifest = {**imported_manifest(manifest), "changed": True}
        if fault == "signature":
            row.manifest_signature = "changed"
        if fault == "source":
            row.git_sha = "c" * 40
        if fault == "image":
            row.image_sha256 = "d" * 64
        if fault == "size":
            row.image_size = 124
        if fault == "key":
            row.signing_key_id = "changed"
        with maker() as session:
            session.add(row)
            session.commit()
    with pytest.raises(ValueError):
        checker.require_published_hil(manifest, signature, published_marker(manifest))
    with maker() as session:
        assert session.scalar(select(func.count()).select_from(Release)) == (
            0 if fault == "missing" else 1
        )


def test_cli_published_hil_option_does_not_skip_cryptographic_check(package, monkeypatch, capsys):
    _, write, _ = package
    paths = write()
    seen = []
    marker = paths[0].parent / ".hil-only.json"
    marker.write_text("{}")
    monkeypatch.setattr(
        checker, "require_published_hil", lambda *args: seen.append(args) or "CATALOG_CURRENT"
    )
    assert (
        checker.main(
            ["--require-published-hil", "--publication-marker", str(marker), *map(str, paths)]
        )
        == 0
    )
    assert len(seen) == 1
    paths[1].write_text("invalid")
    assert (
        checker.main(
            ["--require-published-hil", "--publication-marker", str(marker), *map(str, paths)]
        )
        == 1
    )
    assert len(seen) == 1


def test_imported_catalog_prefix_transition_is_explicit_and_never_mutated(
    package, published_release_database
):
    from sqlalchemy import select

    manifest, write, _ = package
    manifest.update(
        git_sha="a" * 40,
        image_sha256="b" * 64,
        application_sha256="e" * 64,
        image_size=123,
        signing_key_id="key",
    )
    paths = write()
    signature = paths[1].read_text().strip()
    maker, Release = published_release_database
    with maker() as session:
        session.add(
            Release(
                release_id=manifest["release_id"],
                version=manifest["version"],
                git_sha=manifest["git_sha"],
                image_sha256=manifest["image_sha256"],
                image_size=123,
                signing_key_id="key",
                partition_layout="test",
                storage_name="test",
                manifest=imported_manifest(manifest),
                manifest_signature=signature,
                state="HIL_ONLY",
            )
        )
        session.commit()
    assert (
        checker.require_published_hil(manifest, signature, published_marker(manifest))
        == "CATALOG_CURRENT"
    )
    new_marker = published_marker(manifest, 2)
    with pytest.raises(ValueError):
        checker.require_published_hil(manifest, signature, new_marker)
    assert (
        checker.require_published_hil(manifest, signature, new_marker, 1)
        == "CATALOG_REFRESH_PENDING"
    )
    with maker() as session:
        row = session.scalar(select(Release))
        assert row.manifest == imported_manifest(manifest)
        row.manifest = imported_manifest(manifest, 2)
        session.commit()
    assert checker.require_published_hil(manifest, signature, new_marker, 1) == "CATALOG_CURRENT"
    for key, value in [
        ("_publication_mode", "AVAILABLE"),
        ("_hil_target_mac", "00:11:22:33:44:55"),
        ("_hil_targets", signed_hil_targets()[1:2]),
        ("_revoked", True),
    ]:
        with maker() as session:
            row = session.scalar(select(Release))
            row.manifest = {**imported_manifest(manifest, 2), key: value}
            session.commit()
        with pytest.raises(ValueError):
            checker.require_published_hil(manifest, signature, new_marker, 1)
