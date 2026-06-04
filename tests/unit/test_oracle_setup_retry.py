from pathlib import Path


def test_oracle_setup_routes_use_sqlite_retry_wrapper():
    source = Path("apps/zone_agent/zk_zone_agent/web.py").read_text()

    assert "error_response = run_session_with_retries(_save)" in source
    assert "run_session_with_retries(_clear)" in source
