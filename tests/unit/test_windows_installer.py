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
    assert "http://localhost:7860/setup" in inno
    assert "http://127.0.0.1:7860/setup" not in inno
    assert "remove ZKZoneAgentService confirm" in inno
    assert "nssm.cc/release" in build
    assert "choco install nssm" in build
    assert "Find-NssmExe" in build
    assert "Copy-Item -Force $NssmExe" in build
    assert "--collect-all webauthn" in build


def test_windows_cd_builds_state_life_hr_portable_exe():
    root = Path(__file__).resolve().parents[2]
    build = (root / "installer/windows/build_hr.ps1").read_text()
    cd = (root / ".github/workflows/cd.yml").read_text()

    assert "--name StateLifeHREnrollment" in build
    assert "--onefile" in build
    assert "--noconsole" in build
    assert '--paths "apps\\hr_enrollment"' in build
    assert '--paths "apps\\zone_agent"' in build
    assert '--paths "packages\\common"' in build
    assert "Assert-PythonModule" in build
    assert "--collect-all psutil" in build
    assert "--hidden-import psutil._psutil_windows" in build
    assert "--hidden-import psutil._psutil_common" not in build
    assert "--health-check" in build
    assert "Get-Content $HealthLog -Tail 120" in build
    assert "StateLifeHREnrollment.exe failed its packaged dependency health check" in build
    assert "apps\\hr_enrollment\\zk_hr_enrollment\\__main__.py" in build
    assert "StateLifeHREnrollment.exe" in build
    assert "state-life-hr-enrollment-windows-exe" in cd
    assert "dist/StateLifeHREnrollment.exe" in cd
