from pathlib import Path


def test_windows_installer_uses_nssm_service_wrapper():
    root = Path(__file__).resolve().parents[2]
    inno = (root / "installer/windows/ZKZoneAgent.iss").read_text()
    build = (root / "installer/windows/build.ps1").read_text()

    assert "nssm.exe" in inno
    assert "install ZKZoneAgentService" in inno
    assert "zk-zone-agent-service.exe" not in inno
    assert "AppStdout" in inno
    assert "AppRotateBytes" in inno
    assert "remove ZKZoneAgentService confirm" in inno
    assert "nssm.cc/release" in build
    assert "choco install nssm" in build
    assert "Find-NssmExe" in build
    assert "Copy-Item -Force $NssmExe" in build
