"""The deploy gate's Pakistan public-network check against simulated Globalping.

The real step script is extracted from add-deploy.yml and run with fake `curl`,
`dig` and `sleep` executables, so every assertion pins the shipped gate. The
failed results copy the shapes of live Globalping measurements: the probe keeps
its timings only when it times out, and clears them all (and blames the target)
on every other connection error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "add-deploy.yml"
API = "autoattn.slichealth.com"
UI = "attendancedevices.slichealth.com"
API_BODY = '{"status":"ready","database":true,"redis":true}'
UI_BODY = "<!doctype html><title>Attendance Device Dashboard</title>"
ADD_ADDRESS = "125.209.103.210"
NAMESERVERS = ["hgns1.hostgator.com.", "hgns2.hostgator.com."]
DNS_TIMEOUT = "The measurement timed out during DNS resolution."

# GitHub's ubuntu-latest runners (deploy and CI) ship bash and jq, so the gate is
# never skipped there; a local environment without them skips instead.
pytestmark = pytest.mark.skipif(
    (shutil.which("bash") is None or shutil.which("jq") is None)
    and "GITHUB_ACTIONS" not in os.environ,
    reason="the deploy step runs under bash with jq, as on ubuntu-latest",
)

FAKE_CURL = """
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_GLOBALPING"])
scenario = json.loads((state / "scenario.json").read_text())
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))

def log(entry):
    with (state / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps(entry) + "\\n")

def calls():
    return [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]

if "--data" in args:
    assert url == "https://api.globalping.io/v1/measurements", url
    payload = json.loads(args[args.index("--data") + 1])
    target = payload["target"]
    if payload["locations"][0].get("asn") in scenario["unschedulable_asns"]:
        log({"call": "create-rejected", "payload": payload})
        print("curl: (22) The requested URL returned error: 422", file=sys.stderr)
        sys.exit(22)
    log({"call": "create", "payload": payload})
    attempt = sum(1 for call in calls()
                  if call["call"] == "create" and call["payload"]["target"] == target) - 1
    print(json.dumps({"id": f"{target}~{attempt}", "probesCount": 1}))
elif url == "https://api.globalping.io/v1/probes":
    log({"call": "probes"})
    if scenario["probes"] is None:
        print("curl: (22) The requested URL returned error: 503", file=sys.stderr)
        sys.exit(22)
    print(json.dumps(scenario["probes"]))
elif url.startswith("https://api.globalping.io/v1/measurements/"):
    measurement_id = url.rsplit("/", 1)[1]
    target, attempt = measurement_id.split("~")
    log({"call": "result", "id": measurement_id})
    results = scenario["results"].get(target, [])
    if int(attempt) >= len(results):
        print(f"curl: (22) unexpected measurement {measurement_id}", file=sys.stderr)
        sys.exit(22)
    entry = results[int(attempt)]
    status, items = entry.get("measurement_status", "finished"), entry.get("results", [entry])
    print(json.dumps({"id": measurement_id, "type": "http", "status": status,
                      "target": target, "results": items}))
else:
    print(f"curl: (22) unexpected URL {url}", file=sys.stderr)
    sys.exit(22)
"""

FAKE_DIG = """
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_GLOBALPING"])
dns = json.loads((state / "scenario.json").read_text())["dns"]
args = sys.argv[1:]
names = [arg for arg in args if not arg.startswith(("+", "-", "@")) and arg not in ("A", "NS")]
server = next((arg[1:] for arg in args if arg.startswith("@")), None)
with (state / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({"call": "dig", "server": server, "name": names[0], "args": args}) + "\\n")
answer = dns["nameservers"] if server is None else dns["answers"].get(server)
if answer is None:
    print(";; connection timed out; no servers could be reached")
    sys.exit(9)
if server is None and isinstance(answer, list):
    # dig 9.18 on the runner prints a retried try's error on stdout and still exits 0.
    print(";; communications error to 1.1.1.1#53: timed out")
    answer = [f"{names[0]}.\\t14400\\tIN\\tNS\\t{ns}" for ns in answer]
if isinstance(answer, list):
    answer = "\\n".join(answer)
if answer:
    print(answer)
"""


def probe(asn: int, city: str, country: str = "PK") -> dict:
    return {"continent": "AS", "region": "Southern Asia", "country": country,
            "state": None, "city": city, "asn": asn, "latitude": 33.7,
            "longitude": 73.1, "network": f"Network AS{asn}", "tags": [],
            "resolvers": ["1.1.1.1", "8.8.8.8"]}


NO_TIMINGS = {"total": None, "dns": None, "tcp": None, "tls": None, "firstByte": None,
              "download": None}


def failed(raw_output: str, source: str | None, asn: int | str | None = 150315,
           city: str = "Islamabad", timings: dict | None = None, resolved: str | None = None,
           country: str = "PK", status_code: int | None = None) -> dict:
    """A failed result as the probe reports it; `source=None` omits failureSource."""
    result = {"status": "failed", "resolvedAddress": resolved, "headers": {},
              "rawHeaders": None, "rawBody": None, "rawOutput": raw_output,
              "truncated": False, "statusCode": status_code, "statusCodeName": None,
              "timings": {**NO_TIMINGS, **(timings or {})}, "tls": None}
    if source is not None:
        result["failureSource"] = source
    return {"probe": probe(asn, city, country), "result": result}


def dns_timeout(asn: int = 150315, city: str = "Islamabad") -> dict:
    """The incident (measurement 2paEnnh8XtNg03iu200021KNU): the probe's DNS deadline
    expired, so only the total timing is kept."""
    return failed(DNS_TIMEOUT, "resolver", asn, city, timings={"total": 6001})


def resolver_etimeout(asn: int = 150315, city: str = "Islamabad") -> dict:
    """The probe's resolver itself answered ETIMEOUT: every timing is cleared."""
    return failed(DNS_TIMEOUT, "resolver", asn, city)


def http(status: int, body: str, asn: int = 17557, city: str = "Lahore") -> dict:
    return {"probe": probe(asn, city), "result": {
        "status": "finished", "statusCode": status, "statusCodeName": "",
        "rawHeaders": "", "rawBody": body, "rawOutput": f"HTTP/1.1 {status}\n\n{body}",
        "truncated": False, "headers": {}, "resolvedAddress": ADD_ADDRESS,
        "tls": {"authorized": True, "protocol": "TLSv1.3"},
        "timings": {"total": 180, "dns": 12, "tcp": 30, "tls": 40, "firstByte": 90,
                    "download": 8}}}


def online(asn: int, city: str, country: str = "PK") -> dict:
    """One entry of GET /v1/probes, which nests the location unlike a result."""
    location = probe(asn, city, country)
    return {"version": "0.53.0", "tags": location.pop("tags"),
            "resolvers": location.pop("resolvers"), "location": location}


# Online probes. AS3320 is outside Pakistan and must never be chosen although it
# has the most probes. AS150315 has several probes, so a retry must name a
# different ASN, not a fresh draw. AS17557 has two probes and is preferred over
# the lower-numbered single-probe AS9387; AS9387 then wins the tie with AS23674.
ONLINE = [online(150315, "Islamabad"), online(3320, "Berlin", country="DE"),
          online(3320, "Berlin", country="DE"), online(3320, "Berlin", country="DE"),
          online(150315, "Islamabad"), online(23674, "Islamabad"), online(17557, "Lahore"),
          online(9387, "Karachi"), online(17557, "Lahore")]

HEALTHY_DNS = {"nameservers": NAMESERVERS,
               "answers": {name: ADD_ADDRESS for name in NAMESERVERS}}


def step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    (step,) = [step for step in workflow["jobs"]["verify-public"]["steps"]
               if step["name"] == "Verify API and UI through a Pakistan public-network probe"]
    return step["run"]


def run_gate(tmp_path: Path, results: dict, probes: list | None = ONLINE,
             dns: dict = HEALTHY_DNS, unschedulable_asns: tuple = ()):
    state = tmp_path / "globalping"
    bin_dir = tmp_path / "bin"
    state.mkdir()
    bin_dir.mkdir()
    (state / "scenario.json").write_text(json.dumps(
        {"results": results, "probes": probes, "dns": dns,
         "unschedulable_asns": list(unschedulable_asns)}))
    (state / "calls.jsonl").write_text("")
    for tool, source in (("curl", FAKE_CURL), ("dig", FAKE_DIG)):
        (bin_dir / tool).write_text(f"#!{sys.executable}\n{source}")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for tool in ("curl", "dig", "sleep"):
        (bin_dir / tool).chmod(0o755)
    script = tmp_path / "verify.sh"
    script.write_text(step_script())
    # A `run:` step without `shell:` on ubuntu-latest runs as `bash -e {0}` (the
    # deploy log shows `/usr/bin/bash -e {0}`); the script sets -euo pipefail.
    completed = subprocess.run(
        [shutil.which("bash"), "-e", str(script)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "FAKE_GLOBALPING": str(state)})
    calls = [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]
    return completed, calls


def locations(calls: list, target: str) -> list:
    return [call["payload"]["locations"] for call in calls
            if call["call"] == "create" and call["payload"]["target"] == target]


def kinds(calls: list) -> list:
    return [call["call"] for call in calls]


def test_healthy_hosts_verify_from_one_probe_each_without_retry_checks(tmp_path):
    completed, calls = run_gate(tmp_path, {API: [http(200, API_BODY)], UI: [http(200, UI_BODY)]})
    assert completed.returncode == 0, completed.stderr
    assert locations(calls, API) == locations(calls, UI) == [[{"country": "PK"}]]
    assert "probes" not in kinds(calls)
    assert "dig" not in kinds(calls)
    assert "Verified PK/Lahore -> status 200" in completed.stdout
    assert "Verified on attempt 1/3 by probe_asn=17557" in completed.stdout


@pytest.mark.parametrize("timeout", [dns_timeout, resolver_etimeout],
                         ids=["dns-deadline", "resolver-etimeout"])
def test_probe_dns_timeout_retries_from_the_largest_other_pakistan_network(tmp_path, timeout):
    completed, calls = run_gate(tmp_path, {
        API: [timeout(), http(200, API_BODY, asn=17557, city="Lahore")],
        UI: [http(200, UI_BODY)]})
    assert completed.returncode == 0, completed.stderr
    assert locations(calls, API) == [[{"country": "PK"}], [{"country": "PK", "asn": 17557}]]
    assert locations(calls, UI) == [[{"country": "PK"}]]
    # ADD's own nameservers are asked directly, without recursion, before any
    # replacement probe is chosen.
    digs = [call for call in calls if call["call"] == "dig"]
    assert [(call["server"], call["name"]) for call in digs] == [
        (None, "slichealth.com"), (NAMESERVERS[0], API), (NAMESERVERS[1], API)]
    assert all({"-4", "+norec"} <= set(call["args"]) for call in digs[1:])
    assert kinds(calls).index("probes") > max(i for i, kind in enumerate(kinds(calls))
                                              if kind == "dig")
    # The failed attempt keeps its existing diagnostics.
    for line in ("Pakistan public-network verification failed for autoattn.slichealth.com/health/ready.",
                 "probe=PK/Islamabad status=none",
                 "Globalping measurement id=autoattn.slichealth.com~0",
                 "probe_asn=150315 probe_result=failed timings=",
                 f"probe_error={DNS_TIMEOUT}",
                 "probe_failure_source=resolver",
                 f"Authoritative nameserver {NAMESERVERS[1]} answers {API} -> {ADD_ADDRESS}",
                 "The DNS resolver of probe network(s) [150315] timed out for "
                 "autoattn.slichealth.com; retrying from Pakistan network AS17557."):
        assert line in completed.stderr
    assert "Verified PK/Lahore -> status 200" in completed.stdout
    assert "Verified on attempt 2/3 by probe_asn=17557" in completed.stdout


def test_three_probe_dns_timeouts_still_fail_the_gate(tmp_path):
    completed, calls = run_gate(tmp_path, {
        API: [dns_timeout(), dns_timeout(17557, "Lahore"), resolver_etimeout(9387, "Karachi")],
        UI: [http(200, UI_BODY)]})
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}], [{"country": "PK", "asn": 17557}],
                                     [{"country": "PK", "asn": 9387}]]
    assert locations(calls, UI) == []
    assert kinds(calls).count("probes") == 2
    assert ("The DNS resolver of every Pakistan probe timed out for autoattn.slichealth.com "
            "(3 probes on networks [9387,17557,150315]); ADD was never reached.") \
        in completed.stderr


def test_ui_host_gets_the_same_bounded_probe_replacement(tmp_path):
    completed, calls = run_gate(tmp_path, {
        API: [http(200, API_BODY)],
        UI: [dns_timeout(), dns_timeout(17557, "Lahore"), http(200, UI_BODY, 9387, "Karachi")]})
    assert completed.returncode == 0, completed.stderr
    assert locations(calls, UI) == [[{"country": "PK"}], [{"country": "PK", "asn": 17557}],
                                    [{"country": "PK", "asn": 9387}]]
    assert f"Authoritative nameserver {NAMESERVERS[0]} answers {UI} -> {ADD_ADDRESS}" \
        in completed.stderr
    assert "Verified on attempt 3/3 by probe_asn=9387" in completed.stdout


# Each of these is a result about ADD or its own DNS, or cannot be told apart
# from one, so the gate fails on it exactly as before: no replacement probe.
FINAL_RESULTS = {
    "http-503": http(503, '{"status":"degraded","database":false}'),
    "http-200-wrong-body": http(200, '{"status":"ready","database":false}'),
    # Live measurement 25WY4dM6RI1DeG5cn00021LM5: TLS failed after DNS and TCP.
    "tls-error": failed("C0DCD23E9E7F0000:error:0A00010B:SSL routines:"
                        "tls_validate_record_header:wrong version number", "target", 17557, "Lahore"),
    "connection-refused": failed(f"connect ECONNREFUSED {ADD_ADDRESS}:443", "target"),
    "connection-reset": failed("read ECONNRESET", "target"),
    "enotfound": failed(f"queryA ENOTFOUND {API}", "target"),
    # Live measurement 21DGxP2XsZiugEuyY00021LM5: no A record for the name.
    "enodata": failed(f"queryA ENODATA {API}", "target"),
    "private-ip": failed("Private IP ranges are not allowed.", "target"),
    "tcp-timeout": failed("Request timed out while establishing the TCP connection.", "target",
                          timings={"total": 10000, "dns": 15}, resolved=ADD_ADDRESS),
    "tls-timeout": failed("Request timed out during the TLS handshake.", "target",
                          timings={"total": 10000, "dns": 15, "tcp": 40}, resolved=ADD_ADDRESS),
    "resolver-servfail": failed(f"queryA ESERVFAIL {API}", "resolver"),
    "internal": failed("read ENOBUFS", "internal"),
    "dns-timeout-without-failure-source": failed(DNS_TIMEOUT, None, timings={"total": 6001}),
    "dns-timeout-blamed-on-target": failed(DNS_TIMEOUT, "target", timings={"total": 6001}),
    # A resolver-blamed timeout that nonetheless shows contact with ADD, or comes from a
    # probe the gate cannot attribute to a Pakistan network, is never replaced.
    "resolver-timeout-with-address-and-timings": failed(
        DNS_TIMEOUT, "resolver", resolved=ADD_ADDRESS,
        timings={"total": 15000, "dns": 10, "tcp": 30, "tls": 40}),
    "resolver-timeout-with-address": failed(DNS_TIMEOUT, "resolver", resolved=ADD_ADDRESS,
                                            timings={"total": 6001}),
    "resolver-timeout-with-dns-timing": failed(DNS_TIMEOUT, "resolver",
                                               timings={"total": 6001, "dns": 10}),
    "resolver-timeout-with-tcp-timing": failed(DNS_TIMEOUT, "resolver",
                                               timings={"total": 6001, "tcp": 30}),
    "resolver-timeout-with-status": failed(DNS_TIMEOUT, "resolver", timings={"total": 6001},
                                           status_code=502),
    "resolver-timeout-outside-pakistan": failed(DNS_TIMEOUT, "resolver", city="Delhi",
                                                timings={"total": 6001}, country="IN"),
    "resolver-timeout-without-asn": failed(DNS_TIMEOUT, "resolver", asn=None,
                                           timings={"total": 6001}),
    "resolver-timeout-with-text-asn": failed(DNS_TIMEOUT, "resolver", asn="150315",
                                             timings={"total": 6001}),
}


@pytest.mark.parametrize("result", FINAL_RESULTS.values(), ids=FINAL_RESULTS.keys())
def test_any_other_result_fails_immediately_without_retry(tmp_path, result):
    completed, calls = run_gate(tmp_path, {API: [result, http(200, API_BODY, 9387, "Karachi")],
                                           UI: [http(200, UI_BODY)]})
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}]]
    assert locations(calls, UI) == []
    assert "probes" not in kinds(calls)
    assert "dig" not in kinds(calls)
    assert "Pakistan public-network verification failed for autoattn.slichealth.com/health/ready." \
        in completed.stderr
    assert "retrying" not in completed.stderr


@pytest.mark.parametrize("dns,reason", [
    ({**HEALTHY_DNS, "answers": {NAMESERVERS[0]: ADD_ADDRESS}},
     f"Authoritative nameserver {NAMESERVERS[1]} gave no A record for {API}: "
     ";; connection timed out; no servers could be reached"),
    ({**HEALTHY_DNS, "answers": {NAMESERVERS[0]: ADD_ADDRESS, NAMESERVERS[1]: ""}},
     f"Authoritative nameserver {NAMESERVERS[1]} gave no A record for {API}"),
    ({**HEALTHY_DNS, "answers": {name: "add.example.net." for name in NAMESERVERS}},
     f"Authoritative nameserver {NAMESERVERS[0]} gave no A record for {API}: add.example.net."),
    ({"nameservers": None, "answers": {}}, "Unable to list the nameservers of slichealth.com"),
    ({"nameservers": [], "answers": {}}, "Unable to list the nameservers of slichealth.com"),
], ids=["nameserver-timeout", "nameserver-nodata", "nameserver-no-address",
        "ns-lookup-fails", "no-nameservers"])
def test_probe_dns_timeout_is_not_retried_unless_add_dns_answers(tmp_path, dns, reason):
    completed, calls = run_gate(
        tmp_path, {API: [dns_timeout(), http(200, API_BODY)], UI: [http(200, UI_BODY)]}, dns=dns)
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}]]
    assert "probes" not in kinds(calls)
    assert reason in completed.stderr
    assert "Not replacing the probe: autoattn.slichealth.com did not resolve at its " \
        "authoritative nameservers." in completed.stderr


def test_replacement_probe_that_gets_an_error_from_add_fails_without_a_third_try(tmp_path):
    completed, calls = run_gate(tmp_path, {
        API: [dns_timeout(), http(503, "", asn=17557, city="Lahore"), http(200, API_BODY)],
        UI: [http(200, UI_BODY)]})
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}], [{"country": "PK", "asn": 17557}]]
    assert "probe=PK/Lahore status=503" in completed.stderr


@pytest.mark.parametrize("replacement,reason", [
    ({"measurement_status": "finished",
      "results": [{"probe": probe(17557, "Lahore"),
                   "result": {"status": "offline", "rawOutput": "This probe is offline."}}]},
     "probe=PK/Lahore status=none"),
    ({"measurement_status": "failed", "results": []},
     "Public-network verification ended in state failed for autoattn.slichealth.com."),
    ({"measurement_status": "in-progress", "results": []},
     "Pakistan public-network verification timed out for autoattn.slichealth.com."),
], ids=["probe-offline", "measurement-not-finished", "measurement-never-finishes"])
def test_replacement_measurement_that_does_not_finish_fails_the_gate(tmp_path, replacement,
                                                                     reason):
    completed, calls = run_gate(tmp_path, {
        API: [dns_timeout(), replacement, http(200, API_BODY)], UI: [http(200, UI_BODY)]})
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}], [{"country": "PK", "asn": 17557}]]
    assert reason in completed.stderr


def test_unschedulable_replacement_network_fails_the_gate(tmp_path):
    completed, calls = run_gate(
        tmp_path, {API: [dns_timeout(), http(200, API_BODY)], UI: [http(200, UI_BODY)]},
        unschedulable_asns=(17557,))
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}]]
    assert kinds(calls).count("create-rejected") == 3
    assert "Unable to schedule the public-network verification for autoattn.slichealth.com." \
        in completed.stderr


def test_probe_list_failure_fails_the_gate(tmp_path):
    completed, calls = run_gate(
        tmp_path, {API: [dns_timeout(), http(200, API_BODY)], UI: [http(200, UI_BODY)]},
        probes=None)
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}]]
    assert "probes" in kinds(calls)
    assert "Unable to list the online Globalping probes to replace the probe for " \
        "autoattn.slichealth.com." in completed.stderr


def test_no_other_online_pakistan_network_fails_the_gate(tmp_path):
    completed, calls = run_gate(
        tmp_path, {API: [dns_timeout(), http(200, API_BODY)], UI: [http(200, UI_BODY)]},
        probes=[online(150315, "Islamabad"), online(150315, "Islamabad"),
                online(3320, "Berlin", country="DE")])
    assert completed.returncode != 0
    assert locations(calls, API) == [[{"country": "PK"}]]
    assert "probes" in kinds(calls)
    assert "No other Pakistan probe network is online to verify autoattn.slichealth.com" \
        in completed.stderr
