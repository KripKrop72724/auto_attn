import json
import os
from pathlib import Path

import httpx
import pytest

from zk_add.hikvision_probe import (
    MAX_BODY,
    MAX_PART,
    READ_PATHS,
    SEARCH_PATHS,
    STREAM_PATH,
    MultipartDecoder,
    ProbeClient,
    ProbeConfig,
    ProbeError,
    Sanitizer,
    decode_body,
    main,
    multipart_boundary,
    qualify,
    search_pages,
)


def config(**overrides):
    return ProbeConfig(**{
        "base_url": "http://192.168.50.10", "username": "probe-admin",
        "password": "local-secret", "expected_serial": "TEST-SERIAL",
        "allow_http_digest": True, **overrides,
    })


def mime_part(body, content_type=b"application/json"):
    return b"--probe\r\nContent-Type: " + content_type + b"\r\n\r\n" + body + b"\r\n"


def test_untyped_event_log_from_real_terminal_format_is_not_discarded():
    body = b'{"AccessControllerEvent":{"serialNo":123,"currentEvent":true}}'
    message = (b'--probe\r\nContent-Disposition: form-data; name="event_log"\r\n\r\n'
               + body + b'\r\n--probe\r\nContent-Disposition: form-data; name="picture"'
               b'\r\n\r\nbinary\r\n--probe\r\nContent-Disposition: form-data; name="event_log"'
               b'\r\nContent-Type: image/jpeg\r\n\r\nbinary\r\n--probe--\r\n')
    for size in range(1, len(message) + 1):
        decoder = MultipartDecoder(b"probe")
        parts = []
        for offset in range(0, len(message), size):
            parts.extend(decoder.feed(message[offset:offset + size]))
        assert parts == [body]
        assert decoder.discarded_parts == 2


def page(rows, total, status="OK", kind="events"):
    root, key = ("AcsEvent", "InfoList") if kind == "events" else ("UserInfoSearch", "UserInfo")
    return {root: {key: rows, "numOfMatches": len(rows), "totalMatches": total,
                   "responseStatusStrg": status}}


@pytest.mark.parametrize("overrides", [
    {"base_url": "http://192.168.1.2", "allow_http_digest": False},
    {"base_url": "https://example.com"},
    {"base_url": "https://8.8.8.8"},
    {"base_url": "https://127.0.0.1"},
    {"base_url": "https://169.254.169.254"},
    {"base_url": "https://224.0.0.1"},
    {"base_url": "https://192.168.1.2/path"},
    {"base_url": "https://user:secret@192.168.1.2"},
    {"base_url": "https://192.168.1.2:99999"},
    {"base_url": "https://192.168.1.2?password=bad"},
    {"expected_serial": ""}, {"password": ""}, {"timeout_seconds": 0},
    {"allow_http_digest": "false"}, {"timeout_seconds": True},
])
def test_config_rejects_unsafe_or_ambiguous_inputs(overrides):
    with pytest.raises(ProbeError):
        config(**overrides)


def test_config_supports_private_ipv6_and_keeps_secrets_out_of_repr():
    value = config(base_url="https://[fd00::1234]:8443", allow_http_digest=False)
    assert "local-secret" not in repr(value)
    assert "probe-admin" not in repr(value)
    assert "TEST-SERIAL" not in repr(value)


def write_config(path):
    path.write_text(json.dumps({
        "base_url": "http://192.168.50.10", "username": "probe-admin",
        "password": "local-secret", "expected_serial": "TEST-SERIAL",
        "allow_http_digest": True,
    }))
    path.chmod(0o600)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner permissions")
def test_config_requires_private_regular_file_and_rejects_symlink(tmp_path):
    path = tmp_path / "config.json"
    write_config(path)
    assert ProbeConfig.load(path).expected_serial == "TEST-SERIAL"
    path.chmod(0o644)
    with pytest.raises(ProbeError, match="OWNER_ONLY"):
        ProbeConfig.load(path)
    path.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        ProbeConfig.load(link)


def test_sanitization_uses_correlatable_aliases_without_names_or_credentials():
    sanitizer = Sanitizer()
    sample = {"employeeNoString": "000123", "name": "Person Name", "password": "secret",
              "pictureURL": "http://private/photo", "newSensitiveField": "unexpected",
              "dateTime": "2026-09-16T10:00:00+05:00", "major": 5, "serialNo": 700}
    clean = sanitizer.clean(sample)
    rendered = json.dumps(clean)
    for value in ("000123", "Person Name", "secret", "http://private/photo", "unexpected",
                  "2026-09-16T10:00:00+05:00"):
        assert value not in rendered
    assert clean["employeeNoString"] == sanitizer.clean({"employeeNo": "000123"})["employeeNo"]
    assert clean["employeeNoString"] != Sanitizer().clean(sample)["employeeNoString"]
    assert clean["major"] == 5 and clean["serialNo"] == 700


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096, 200_000])
def test_multipart_handles_fragmentation_images_and_boundary_like_bytes(chunk_size):
    decoder = MultipartDecoder(b"probe")
    source = (mime_part(b'{"serialNo":1}')
              + mime_part(b"jpeg" * 30_000 + b"\r\n--probeX!still-image", b"image/jpeg")
              + mime_part(b'{"serialNo":2}') + b"--probe--\r\n")
    output = []
    max_buffer = 0
    for offset in range(0, len(source), chunk_size):
        output.extend(decoder.feed(source[offset:offset + chunk_size]))
        max_buffer = max(max_buffer, len(decoder.buffer) + len(decoder.body))
    assert [json.loads(value) for value in output] == [{"serialNo": 1}, {"serialNo": 2}]
    assert decoder.discarded_parts == 1
    assert max_buffer < MAX_PART + 8192


def test_oversized_metadata_is_discarded_and_next_part_recovers():
    decoder = MultipartDecoder(b"probe")
    output = list(decoder.feed(mime_part(b"x" * (MAX_PART + 1))
                              + mime_part(b'{"valid":true}') + b"--probe--\r\n"))
    assert output == [b'{"valid":true}']
    assert decoder.oversized_parts == 1


def test_bad_headers_and_boundaries_fail_bounded():
    assert multipart_boundary('multipart/mixed; boundary="probe"') == b"probe"
    with pytest.raises(ProbeError):
        multipart_boundary("application/json")
    with pytest.raises(ProbeError):
        MultipartDecoder(b"a\r\nb")
    with pytest.raises(ProbeError, match="HEADERS_TOO_LARGE"):
        list(MultipartDecoder(b"probe").feed(b"--probe\r\n" + b"x" * 10_000))


def test_decode_handles_namespaced_xml_and_blocks_entities_and_oversized_bodies():
    value = decode_body(b'<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                        b"<model>DS-K1T342EFWX</model></DeviceInfo>")
    assert value == {"DeviceInfo": {"model": "DS-K1T342EFWX"}}
    for body in (b'<!DOCTYPE x [<!ENTITY a "boom">]><x>&a;</x>', b"{bad", b"[]",
                 b"x" * (MAX_BODY + 1), '<!DOCTYPE x><x/>'.encode("utf-16")):
        with pytest.raises(ProbeError):
            decode_body(body)


def test_search_uses_returned_count_not_requested_page_size():
    positions = []

    def fetch(payload):
        cond = payload["AcsEventCond"]
        positions.append(cond["searchResultPosition"])
        assert cond["picEnable"] is False
        assert cond["major"] == cond["minor"] == 0
        if len(positions) == 1:
            return page([{"serialNo": 1}], 3, "MORE")
        return page([{"serialNo": 3}, {"serialNo": 9}], 3)

    result = list(search_pages(fetch, kind="events", page_size=20))
    assert positions == [0, 1]
    assert result[-1][1]["enumeration_complete"] is True


@pytest.mark.parametrize("response,error", [
    (page([], 0, "MORE"), "INCONSISTENT_END"),
    (page([], 2, "MORE"), "STALLED"),
    (page([{}], 2, "OK"), "PREMATURE_END"),
    (page([{}], 2, "NO MATCH"), "STALLED"),
    ({"AcsEvent": {"InfoList": [], "numOfMatches": "0", "totalMatches": 0}}, "INVALID_PAGE"),
    ({"ResponseStatus": {"statusCode": 4}}, "RESULT_MISSING"),
    ([], "RESULT_MISSING"),
])
def test_search_rejects_inconsistent_pages(response, error):
    with pytest.raises(ProbeError, match=error):
        list(search_pages(lambda _: response, kind="events"))


def test_search_detects_repetition_changing_totals_and_session_mismatch():
    for responses, error in (
        ([page([{"serialNo": 1}], 3, "MORE")] * 2, "REPEATED_PAGE"),
        ([page([{"serialNo": 1}], 3, "MORE"), page([{"serialNo": 2}], 4, "MORE")],
         "TOTAL_CHANGED"),
        ([{"AcsEvent": {**page([], 0)["AcsEvent"], "searchID": "other"}}],
         "SESSION_MISMATCH"),
    ):
        items = iter(responses)
        with pytest.raises(ProbeError, match=error):
            list(search_pages(lambda _: next(items), kind="events"))


def test_search_sample_never_claims_complete_and_preserves_leading_zeros():
    rows, cursor = next(search_pages(
        lambda _: page([{"employeeNo": "00012"}], 100, "MORE", "users"),
        kind="users", max_pages=1,
    ))
    assert rows[0]["employeeNo"] == "00012"
    assert cursor["enumeration_complete"] is False


def test_150000_event_search_is_bounded_and_does_not_assume_contiguous_serials():
    calls = 0
    peak_rows = 0

    def fetch(payload):
        nonlocal calls, peak_rows
        condition = payload["AcsEventCond"]
        start = condition["searchResultPosition"]
        count = min(condition["maxResults"], 150_000 - start)
        rows = [{"serialNo": i * 7, "employeeNoString": "00012"}
                for i in range(start, start + count)]
        peak_rows = max(peak_rows, len(rows))
        calls += 1
        return page(rows, 150_000, "OK" if start + count == 150_000 else "MORE")

    processed = 0
    last = None
    for rows, cursor in search_pages(fetch, kind="events", max_pages=7500):
        processed += len(rows)
        last = cursor
    assert processed == 150_000 and calls == 7500 and peak_rows == 20
    assert last["enumeration_complete"] is True


def test_read_only_allowlist_prevents_all_writes_without_network():
    def forbidden(_):
        pytest.fail("must reject before making a request")

    client = ProbeClient(config(), transport=httpx.MockTransport(forbidden))
    try:
        for method, path in (("PUT", "/ISAPI/AccessControl/UserInfo/Modify?format=json"),
                             ("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json"),
                             ("DELETE", SEARCH_PATHS["users"]),
                             ("GET", "http://8.8.8.8/")):
            with pytest.raises(ProbeError, match="READ_ONLY"):
                client.request(method, path)
    finally:
        client.close()


def test_http_digest_challenge_and_no_redirect_or_raw_error_leak():
    attempts = []

    def handler(request):
        attempts.append(request)
        if "Authorization" not in request.headers:
            return httpx.Response(401, headers={
                "WWW-Authenticate": 'Digest realm="test", nonce="abc", qop="auth", algorithm=MD5',
            })
        assert request.headers["Authorization"].startswith("Digest ")
        assert "local-secret" not in request.headers["Authorization"]
        return httpx.Response(200, json={"DeviceInfo": {"model": "test"}})

    client = ProbeClient(config(), transport=httpx.MockTransport(handler))
    try:
        assert client.request("GET", READ_PATHS[0])["DeviceInfo"]["model"] == "test"
        assert len(attempts) == 2
    finally:
        client.close()

    def redirect(_):
        return httpx.Response(302, headers={"Location": "https://external.invalid/secret"},
                              text="sensitive password")

    client = ProbeClient(config(), transport=httpx.MockTransport(redirect))
    try:
        with pytest.raises(ProbeError, match="^HTTP_302$"):
            client.request("GET", READ_PATHS[0])
    finally:
        client.close()


def device_handler(request):
    path = request.url.path
    if path == READ_PATHS[0]:
        return httpx.Response(200, content=(
            b"<DeviceInfo><serialNumber>TEST-SERIAL</serialNumber>"
            b"<model>DS-K1T342EFWX</model><firmwareVersion>V3.3.5</firmwareVersion>"
            b"</DeviceInfo>"))
    if path == STREAM_PATH:
        return httpx.Response(200, headers={"Content-Type": "multipart/mixed; boundary=probe"},
                              content=mime_part(b'{"eventType":"AccessControllerEvent",'
                                                b'"employeeNoString":"00012"}') + b"--probe--\r\n")
    if path == SEARCH_PATHS["users"].split("?")[0]:
        return httpx.Response(200, json=page([{"employeeNo": "00012", "name": "Private Name"}],
                                            1, kind="users"))
    if path == SEARCH_PATHS["events"].split("?")[0]:
        return httpx.Response(200, json=page([], 0, "NO MATCH"))
    return httpx.Response(200, json={"Capability": {"supported": True}})


def test_end_to_end_report_is_read_only_anonymized_and_never_certifies():
    requests = []

    def handler(request):
        requests.append(request)
        return device_handler(request)

    report = qualify(config(), transport=httpx.MockTransport(handler))
    assert report["identity_verified"] is True
    assert report["qualified"] is False
    assert report["searches"]["users"]["enumeration_complete"] is True
    assert report["searches"]["users"]["coverage_certified"] is False
    assert report["live_stream"]["messages"] == 1
    assert all(status == "NOT_RUN" for status in report["required_hardware_checks"].values())
    encoded = json.dumps(report)
    for sensitive in ("Private Name", "00012", "TEST-SERIAL", "local-secret", "192.168.50.10"):
        assert sensitive not in encoded
    assert all(request.method == "GET" or request.url.path in {
        path.split("?")[0] for path in SEARCH_PATHS.values()
    } for request in requests)


def test_serial_mismatch_stops_before_stream_or_user_search():
    requests = []

    def handler(request):
        requests.append(request)
        return device_handler(request)

    report = qualify(config(expected_serial="different"), transport=httpx.MockTransport(handler))
    assert report["qualification_state"] == "TERMINAL_SERIAL_MISMATCH"
    assert report["identity_verified"] is False
    assert len(requests) == 1


def test_cli_does_not_overwrite_reports_or_print_config_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    write_config(path)
    output = tmp_path / "report.json"
    output.write_text("previous report")
    monkeypatch.setattr("zk_add.hikvision_probe.qualify", lambda *a, **kw: {"identity_verified": True})
    assert main(["--config", str(path), "--output", str(output)]) == 2
    assert output.read_text() == "previous report"
    assert "local-secret" not in capsys.readouterr().out


def test_cli_success_creates_owner_only_report(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    write_config(path)
    output = tmp_path / "report.json"
    monkeypatch.setattr("zk_add.hikvision_probe.qualify", lambda *a, **kw: {
        "identity_verified": True, "qualified": False,
    })
    assert main(["--config", str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["qualified"] is False
    if os.name == "posix":
        assert Path(output).stat().st_mode & 0o777 == 0o600


def test_digest_reauthenticates_after_nonce_change():
    calls = []
    nonce = "first"

    def handler(request):
        authorization = request.headers.get("authorization", "")
        calls.append(authorization)
        if f'nonce="{nonce}"' not in authorization:
            return httpx.Response(401, headers={
                "WWW-Authenticate": f'Digest realm="test", nonce="{nonce}", '
                                    'qop="auth", algorithm=MD5, stale=true',
            })
        return httpx.Response(200, json={"DeviceInfo": {"model": "test"}})

    client = ProbeClient(config(), transport=httpx.MockTransport(handler))
    try:
        client.request("GET", READ_PATHS[0])
        nonce = "second"
        client.request("GET", READ_PATHS[0])
        assert len(calls) == 4
        assert 'nonce="second"' in calls[-1]
    finally:
        client.close()


def test_read_only_search_recovers_one_rejected_nonce_exchange():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 4:
            nonce = "first" if len(calls) == 1 else "renewed"
            return httpx.Response(401, headers={
                "WWW-Authenticate": f'Digest realm="test", nonce="{nonce}", qop="auth", algorithm=MD5',
            })
        return httpx.Response(200, json={"AcsEvent": {"totalMatches": 0}})

    client = ProbeClient(config(), transport=httpx.MockTransport(handler))
    try:
        assert client.request("POST", SEARCH_PATHS["events"], {"AcsEventCond": {}}) == {
            "AcsEvent": {"totalMatches": 0},
        }
        assert len(calls) == 4
        assert 'nonce="renewed"' in calls[-1].headers["authorization"]
    finally:
        client.close()


def test_reauthentication_is_bounded_on_bad_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, headers={
            "WWW-Authenticate": 'Digest realm="test", nonce="denied", qop="auth", algorithm=MD5',
        })

    client = ProbeClient(config(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProbeError, match="HTTP_401"):
            client.request("GET", READ_PATHS[0])
        assert len(calls) == 4
    finally:
        client.close()


def test_unauthorized_capability_stops_more_requests():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == READ_PATHS[0]:
            return device_handler(request)
        return httpx.Response(403, text="private credentials error")

    report = qualify(config(), transport=httpx.MockTransport(handler))
    assert report["qualification_state"] == "AUTHORIZATION_FAILED"
    assert len(requests) == 2
    assert "private credentials error" not in json.dumps(report)


def test_unexpected_identity_shape_is_reported_without_traceback():
    report = qualify(config(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"DeviceInfo": None}),
    ))
    assert report["qualification_state"] == "TERMINAL_SERIAL_MISMATCH"


def test_request_rejects_oversized_body_and_hides_transport_errors():
    client = ProbeClient(config(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * (MAX_BODY + 1)),
    ))
    try:
        with pytest.raises(ProbeError, match="BODY_TOO_LARGE"):
            client.request("GET", READ_PATHS[0])
    finally:
        client.close()

    def failed(_):
        raise httpx.ConnectError("192.168.50.10 secret-containing-error")

    report = qualify(config(), transport=httpx.MockTransport(failed))
    assert report["qualification_state"] == "NETWORK_OR_TLS_ERROR"
    assert "secret-containing-error" not in json.dumps(report)


def test_echoed_credentials_are_redacted_even_in_safe_fields_or_keys():
    sanitizer = Sanitizer(("test-password",))
    value = sanitizer.clean({"model": "echo test-password", "test-password": True})
    assert "test-password" not in json.dumps(value)


def test_secure_setup_hides_password_and_never_contacts_device(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private.json"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["http://192.168.50.10", "test-user", "TEST-SERIAL", "yes"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "test-secret-password")
    assert main(["--init-config", str(path)]) == 0
    assert ProbeConfig.load(path).password == "test-secret-password"
    assert "test-secret-password" not in capsys.readouterr().out


def test_secure_setup_refuses_noninteractive_input(tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--init-config", str(path)]) == 2
    assert not path.exists()


def test_capability_ranges_and_xml_attributes_survive_without_unmasking_persons():
    xml = b'<Cap version="2.0"><employeeNo min="1" max="32"/><supported>true</supported></Cap>'
    value = decode_body(xml)
    clean = Sanitizer().clean(value, capabilities=True)
    assert clean["Cap"]["employeeNo"] == {"@min": "1", "@max": "32"}
    assert clean["Cap"]["supported"] is True
    assert Sanitizer().clean({"employeeNo": "0012"}, capabilities=True)["employeeNo"] != "0012"


def test_serial_range_search_resumes_with_fresh_session_without_assuming_no_gaps():
    conditions = []

    def fetch(payload):
        cond = payload["AcsEventCond"]
        conditions.append(cond)
        pos = cond["searchResultPosition"]
        return page([{"serialNo": n} for n in ([30001, 30004] if pos == 0 else [30020])],
                    3, "MORE" if pos == 0 else "OK")

    for _ in range(2):
        result = list(search_pages(fetch, kind="events", serial_range=(30001, 30020)))
        assert result[-1][1]["enumeration_complete"]
    assert conditions[0]["searchID"] != conditions[2]["searchID"]
    assert all(row["beginSerialNo"] == 30001 and row["endSerialNo"] == 30020
               for row in conditions)
    assert [row["searchResultPosition"] for row in conditions] == [0, 2, 0, 2]


@pytest.mark.parametrize("serials", [[30000], [30021], [30002, 30001], [30001, 30001], ["30001"]])
def test_serial_range_rejects_outside_repeated_reversed_or_untyped_keys(serials):
    with pytest.raises(ProbeError, match="SERIAL_BOUNDARY_OR_ORDER"):
        list(search_pages(lambda _: page([{"serialNo": n} for n in serials], len(serials)),
                          kind="events", serial_range=(30001, 30020)))


def test_current_punches_are_not_hidden_by_initial_stream_replay():
    payload = b"".join(mime_part(json.dumps({
        "AccessControllerEvent": {"serialNo": serial, "currentEvent": current},
    }).encode()) for serial, current in [(1, False)] * 15 + [(20, True), (21, None)])
    client = ProbeClient(config(), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"Content-Type": "multipart/mixed; boundary=probe"},
        content=payload + b"--probe--\r\n",
    )))
    try:
        result = client.observe_stream(1, Sanitizer())
        assert result["replayed_event_count"] == 15
        assert result["current_event_count"] == result["unknown_age_event_count"] == 1
        assert result["current_samples"][0]["AccessControllerEvent"]["serialNo"] == 20
    finally:
        client.close()


def test_xml_stream_boolean_flags_and_non_event_parts():
    payload = b"".join(mime_part(
        b'<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">'
        b'<AccessControllerEvent><serialNo>20</serialNo><currentEvent>' + flag
        + b'</currentEvent></AccessControllerEvent></EventNotificationAlert>',
        b"application/xml",
    ) for flag in (b"true", b"false", b"unknown"))
    payload += mime_part(b'{"EventNotificationAlert": null}')
    client = ProbeClient(config(), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"Content-Type": "multipart/mixed; boundary=probe"},
        content=payload + b"--probe--\r\n",
    )))
    try:
        result = client.observe_stream(1, Sanitizer())
        assert result["messages"] == 4
        assert result["current_event_count"] == 1
        assert result["replayed_event_count"] == 1
        assert result["unknown_age_event_count"] == 1
        assert len(result["current_samples"]) == 1
    finally:
        client.close()
