"""Real synthetic RSA/SBv2 packages; no production keys, stores or catalog."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
from uuid import uuid4
import zlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from sqlalchemy import create_engine, event, text

from zk_add import db, zkt_reader_matrix as policy
from zk_add.settings import settings
from zk_add.zkt_bridge_contract import bridge_contract, bridge_marker, signed_hil_targets

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/check_deployed_reader_packages.py'
spec = importlib.util.spec_from_file_location('reader_packages', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def signed_image(version, key, *, marker=None):
    header = bytearray(24)
    header[0], header[1], header[23] = 0xE9, 1, 1
    struct.pack_into('<H', header, 12, 9)
    payload = bytearray(512)
    struct.pack_into('<I', payload, 0, 0xABCD5432)
    payload[16:16 + len(version)] = version.encode()
    payload[48:57] = b'zone_lite'
    compiled = ((marker if marker is not None else bridge_marker(version)) + '\0').encode()
    payload[256:256 + len(compiled)] = compiled
    raw = bytes(header) + struct.pack('<II', 0x3C000020, len(payload)) + bytes(payload)
    checksum = 0xEF
    for byte in payload:
        checksum ^= byte
    end = (len(raw) + 16) // 16 * 16
    raw += b'\0' * (end - len(raw) - 1) + bytes([checksum])
    raw += hashlib.sha256(raw).digest()
    prefix = raw.ljust((len(raw) + 4095) // 4096 * 4096, b'\xff')
    digest = hashlib.sha256(prefix).digest()
    public = key.public_key().public_numbers()
    signature = key.sign(digest, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                         utils.Prehashed(hashes.SHA256()))
    block = struct.pack('<BBxx32s384sI384sI384s', 0xE7, 2, digest,
        public.n.to_bytes(384, 'little'), public.e, pow(2, 6144, public.n).to_bytes(384, 'little'),
        (-pow(public.n, -1, 1 << 32)) & 0xFFFFFFFF, signature[::-1])
    block += struct.pack('<I', zlib.crc32(block) & 0xFFFFFFFF) + b'\0' * 16
    return prefix + block.ljust(4096, b'\xff')


@pytest.fixture
def packages(tmp_path, key, monkeypatch):
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    entries, records = [], []
    # Existing roles exercise the actual installed bridge validators. The
    # production policy stays BLOCKED; exact 21/22 shape has separate tests.
    monkeypatch.setattr(policy, 'VERSIONS', ('2.6.21', '2.6.20'))
    for version in policy.VERSIONS:
        image = signed_image(version, key)
        entry = dict(version=version, release_id='zone-lite-' + version,
            source_sha=hashlib.sha1(version.encode()).hexdigest(),
            application_sha256=checker.application_digest(image, version),
            artifact_sha256=hashlib.sha256(image).hexdigest(), signing_key_id=hashlib.sha256(pem).hexdigest())
        manifest = dict(schema_version=3, version=version, release_id=entry['release_id'],
            git_sha=entry['source_sha'], firmware_family='zkt', project_name='zone_lite',
            release_channel='EXPERIMENTAL_HIL_ONLY', minimum_bootstrap_version='2.4.12',
            partition_layout='zone-lite-ota-v1', image_name='zone-lite-' + version + '.bin',
            image_sha256=entry['artifact_sha256'], image_size=len(image),
            application_sha256=entry['application_sha256'], signing_key_id=entry['signing_key_id'],
            hil_targets=signed_hil_targets(), queue_storage=bridge_contract(version))
        marker = dict(schema_version=2, version=version, git_sha=entry['source_sha'],
                      image_sha256=entry['artifact_sha256'], application_sha256=entry['application_sha256'],
                      targets=signed_hil_targets()[:1])
        directory = tmp_path / version
        directory.mkdir()
        (directory / manifest['image_name']).write_bytes(image)
        (directory / '.hil-only.json').write_bytes(policy.canonical(marker))
        record = dict(manifest, storage_name=version + '/' + manifest['image_name'],
                      manifest={**manifest, '_publication_mode': 'HIL_ONLY', '_hil_targets': marker['targets']},
                      state='HIL_ONLY', revoked_at=None)
        entries.append(entry)
        records.append(record)
    matrix = dict(schema_version=1, matrix_id=policy.MATRIX_ID, state='PINNED', readers=entries)
    matrix_path = tmp_path / 'matrix.json'
    monkeypatch.setattr(policy, 'MATRIX_PATH', matrix_path)
    monkeypatch.setattr(settings, 'firmware_store_path', str(tmp_path))
    monkeypatch.setattr(settings, 'firmware_signing_public_key_pem_b64', base64.b64encode(pem).decode())

    def save(index=0, image=None):
        entry, record = entries[index], records[index]
        manifest = {k: v for k, v in record['manifest'].items() if not k.startswith('_')}
        if image is not None:
            entry['artifact_sha256'] = record['image_sha256'] = manifest['image_sha256'] = hashlib.sha256(image).hexdigest()
            # Malformed descriptors are tested at the verifier, not repaired here.
            manifest['application_sha256'] = entry['application_sha256']
            (tmp_path / entry['version'] / manifest['image_name']).write_bytes(image)
            record['manifest'].update(manifest)
        raw = policy.canonical(manifest)
        signature = base64.b64encode(key.sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
            salt_length=32), hashes.SHA256())).decode()
        record['manifest_signature'] = signature
        (tmp_path / entry['version'] / 'manifest.json').write_bytes(raw)
        (tmp_path / entry['version'] / 'manifest.sig').write_text(signature)
        matrix_path.write_bytes(policy.canonical(matrix))

    save(0)
    save(1)
    return tmp_path, entries, records, pem, matrix, save


def test_signed_packages_verify_without_modifying_store(packages):
    root, entries, records, pem, _, _ = packages
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob('*') if p.is_file()}
    for entry, record in zip(entries, records):
        checker.verify_package(root, entry, record, pem)
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault', ['absent', 'revoked', 'revoked_time', 'source', 'key', 'storage_path',
    'missing_image', 'symlink', 'oversize', 'bad_manifest_sig', 'duplicate_metadata', 'catalog_drift',
    'scope_drift', 'wrong_trust', 'signed_padding', 'signature_block', 'compiled_marker', 'wrong_descriptor'])
def test_invalid_or_incomplete_proof_is_refused(packages, key, fault):
    root, entries, records, pem, _, save = packages
    entry, record = entries[0], records[0]
    directory = root / entry['version']
    image_path = directory / record['manifest']['image_name']
    if fault == 'absent':
        record = None
    elif fault == 'revoked':
        record['state'] = 'REVOKED'
    elif fault == 'revoked_time':
        record['revoked_at'] = '2026-01-01'
    elif fault in {'source', 'key', 'storage_path'}:
        record[{'source': 'git_sha', 'key': 'signing_key_id', 'storage_path': 'storage_name'}[fault]] = 'private-unknown-value'
    elif fault == 'missing_image':
        image_path.unlink()
    elif fault == 'symlink':
        image_path.rename(root / 'external.bin')
        image_path.symlink_to(root / 'external.bin')
    elif fault == 'oversize':
        with image_path.open('r+b') as stream:
            stream.truncate(0x280000)
    elif fault == 'bad_manifest_sig':
        record['manifest_signature'] = 'cHJpdmF0ZQ=='
        (directory / 'manifest.sig').write_text(record['manifest_signature'])
    elif fault == 'duplicate_metadata':
        (directory / 'manifest.json').write_text('{"version":"2.6.21","version":"private"}')
    elif fault == 'catalog_drift':
        record['manifest']['application_sha256'] = 'e' * 64
    elif fault == 'scope_drift':
        record['manifest']['_hil_targets'] = []
    elif fault == 'wrong_trust':
        pem = pem + b'\n'
    elif fault in {'signed_padding', 'signature_block'}:
        image = bytearray(image_path.read_bytes())
        image[1024 if fault == 'signed_padding' else 4096 + 812] ^= 1
        save(image=bytes(image))  # Re-pin/resign metadata: SBv2 must still reject.
    elif fault == 'compiled_marker':
        image = signed_image(entry['version'], key, marker='UNQUALIFIED')
        entry['application_sha256'] = checker.application_digest(image, entry['version'])
        save(image=image)
    elif fault == 'wrong_descriptor':
        save(image=signed_image(entries[1]['version'], key))
    with pytest.raises(Exception):
        checker.verify_package(root, entry, record, pem)


@pytest.fixture(params=['sqlite', 'postgres'])
def catalog(request, monkeypatch):
    admin = None
    if request.param == 'postgres':
        url = os.environ.get('ADD_SAFE_REPAIR_TEST_DATABASE_URL') or (os.environ.get('ADD_DATABASE_URL') if os.environ.get('CI') else None)
        if not url or not url.startswith('postgresql'):
            pytest.skip('Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for isolated PostgreSQL')
        schema = 'reader_package_test_' + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema} -cstatement_timeout=30000'})
    else:
        engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE add_firmware_releases (release_id TEXT PRIMARY KEY, version TEXT, '
            'git_sha TEXT, image_sha256 TEXT, image_size INTEGER, signing_key_id TEXT, partition_layout TEXT, '
            'storage_name TEXT, manifest TEXT, manifest_signature TEXT, state TEXT, revoked_at TEXT)'))
    monkeypatch.setattr(db, 'engine', engine)
    yield engine
    engine.dispose()
    if admin:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize('fault', [None, 'second_missing', 'second_revoked', 'policy_drift'])
def test_pre_sign_check_requires_every_package_and_uses_read_only_queries(packages, catalog, fault, capsys):
    _, _, records, _, matrix, _ = packages
    with catalog.begin() as connection:
        for index, record in enumerate(records):
            if index == 1 and fault == 'second_missing':
                continue
            row = deepcopy(record)
            if index == 1 and fault == 'second_revoked':
                row['state'] = 'REVOKED'
            row['manifest'] = json.dumps(row['manifest'])
            columns = ('release_id version git_sha image_sha256 image_size signing_key_id partition_layout '
                       'storage_name manifest manifest_signature state revoked_at').split()
            connection.execute(text('INSERT INTO add_firmware_releases (' + ','.join(columns) + ') VALUES ('
                                    + ','.join(':' + name for name in columns) + ')'), row)
    statements = []
    event.listen(catalog, 'before_cursor_execute', lambda c, u, statement, p, x, m: statements.append(statement))
    digest = '0' * 64 if fault == 'policy_drift' else policy.matrix_hash(matrix)
    assert checker.main([digest]) == int(fault is not None)
    assert capsys.readouterr() == (('ADD_READER_PACKAGES_REJECTED' if fault else
                                  'ADD_READER_PACKAGES_ACCEPTED:' + digest) + '\n', '')
    assert all(s.startswith(('SELECT ', 'SET TRANSACTION READ ONLY', 'PRAGMA query_only=ON')) for s in statements)
    assert len([s for s in statements if s.startswith('SELECT ')]) == (0 if fault == 'policy_drift' else 2)
