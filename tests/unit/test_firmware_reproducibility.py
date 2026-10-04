import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("firmware_reproducibility",
    ROOT / "scripts/check_firmware_reproducibility.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def builds(tmp_path):
    directories = (tmp_path / "first", tmp_path / "second")
    for directory in directories:
        (directory / "config").mkdir(parents=True)
        (directory / "config/sdkconfig.h").write_text("#define CONFIG_APP_REPRODUCIBLE_BUILD 1\n")
        for relative in checker.ARTIFACTS:
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic-build-artifact:" + relative.encode())
    return directories


def test_reports_only_hashes_after_every_required_artifact_matches(builds):
    evidence = checker.compare(*builds)
    assert set(evidence) == set(checker.ARTIFACTS)
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in evidence.values())


@pytest.mark.parametrize("relative", checker.ARTIFACTS)
def test_any_changed_artifact_prevents_a_reproducibility_result(builds, relative):
    (builds[1] / relative).write_bytes(b"changed")
    with pytest.raises(ValueError, match="REPRODUCIBLE_BUILD_MISMATCH"):
        checker.compare(*builds)


@pytest.mark.parametrize("definition", ["", "#define CONFIG_APP_COMPILE_TIME_DATE 1\n",
                                      "#define CONFIG_BOOTLOADER_COMPILE_TIME_DATE 1\n"])
def test_time_sensitive_or_missing_configuration_is_not_a_proof(builds, definition):
    configuration = definition and "#define CONFIG_APP_REPRODUCIBLE_BUILD 1\n" + definition
    (builds[1] / "config/sdkconfig.h").write_text(configuration)
    with pytest.raises(ValueError, match="REPRODUCIBLE_BUILD_CONFIGURATION_REQUIRED"):
        checker.compare(*builds)


def test_reusing_the_same_build_or_artifact_is_rejected(builds):
    with pytest.raises(ValueError, match="INDEPENDENT_BUILD_DIRECTORIES_REQUIRED"):
        checker.compare(builds[0], builds[0])
    target = builds[1] / "zone_lite.bin"
    target.unlink()
    target.hardlink_to(builds[0] / "zone_lite.bin")
    with pytest.raises(ValueError, match="INDEPENDENT_ARTIFACTS_REQUIRED"):
        checker.compare(*builds)


def test_missing_or_empty_artifact_cannot_pass(builds):
    target = builds[1] / "zone_lite.elf"
    target.unlink()
    with pytest.raises(FileNotFoundError):
        checker.compare(*builds)
    target.write_bytes(b"")
    (builds[0] / "zone_lite.elf").write_bytes(b"")
    with pytest.raises(ValueError, match="REPRODUCIBLE_BUILD_MISMATCH"):
        checker.compare(*builds)
