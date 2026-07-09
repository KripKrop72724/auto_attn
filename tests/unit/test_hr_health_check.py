from zk_hr_enrollment import __main__ as hr_main


def test_health_check_requires_zkemkeeper_payload_when_enabled(monkeypatch):
    imported = []

    def fake_import(module_name):
        imported.append(module_name)

    monkeypatch.setattr(hr_main.platform, "system", lambda: "Windows")
    monkeypatch.setenv("HR_REQUIRE_ZKEMKEEPER_DLL", "1")
    monkeypatch.setattr(hr_main.importlib, "import_module", fake_import)
    monkeypatch.setattr(hr_main, "log_exception", lambda *_args, **_kwargs: None)

    import zk_hr_enrollment.official_sdk as official_sdk

    monkeypatch.setattr(official_sdk, "find_missing_zkemkeeper_payloads", lambda: ("zkemkeeper.dll",))

    assert hr_main._run_health_check() == 1
    assert "zk_hr_enrollment.official_sdk" in imported
