from __future__ import annotations

import logging

import pytest

from sl_installer.core.api import Family
from sl_installer.core.bundles import BundleStore
from sl_installer.core.logging_setup import LOGGER_NAME, forget_secrets

from . import support


@pytest.fixture(scope="session")
def manifest_key():
    """A throwaway RSA-3072 manifest key (the production one is never available)."""
    return support.signing_key()


@pytest.fixture
def zkt_bundle(tmp_path, manifest_key):
    return support.build_bundle(tmp_path, Family.ZKT, manifest_key)


@pytest.fixture
def hik_bundle(tmp_path, manifest_key):
    return support.build_bundle(tmp_path, Family.HIKVISION, manifest_key)


@pytest.fixture
def store(tmp_path, manifest_key):
    return BundleStore(tmp_path / "cache", support.verifier(manifest_key),
                       storage_gz=support.STORAGE_GZ)


@pytest.fixture(autouse=True)
def _clean_logging():
    forget_secrets()
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = True
    yield
    forget_secrets()
