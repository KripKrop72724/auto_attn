"""The old screens stay one click away under /classic/ for one week after the revamp release.

Delete this file with apps/add_frontend_classic (docs/add-classic-screens.md).
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CLASSIC = ROOT / "apps/add_frontend_classic"
MAIN = ROOT / "apps/add_frontend"


def _location(nginx: str, header: str) -> str:
    block = nginx[nginx.index(header):]
    return block[:block.index("}") + 1]


def test_nginx_serves_the_old_screens_with_a_spa_fallback_and_security_headers():
    nginx = (MAIN / "nginx.conf").read_text(encoding="utf-8")
    pages = _location(nginx, "location /classic/ {")
    assert "try_files $uri /classic/index.html;" in pages
    assert "include /etc/nginx/security-headers.conf;" in pages
    assert 'add_header Cache-Control "no-store";' in pages
    assets = _location(nginx, "location /classic/assets/ {")
    assert "include /etc/nginx/security-headers.conf;" in assets
    assert "try_files $uri =404;" in assets
    bare = _location(nginx, "location = /classic {")
    # A relative redirect keeps the public port (8095) that nginx itself cannot see.
    assert "absolute_redirect off;" in bare
    assert "return 302 /classic/;" in bare
    # The new app keeps its own fallback; /status and /api are untouched.
    assert "try_files $uri $uri/ /index.html;" in _location(nginx, "location / {")
    assert nginx.index("location /classic/ {") < nginx.index("location / {")


def test_the_security_policy_still_allows_only_same_origin_scripts():
    headers = (MAIN / "security-headers.conf").read_text(encoding="utf-8")
    assert "script-src 'self'" in headers
    assert "style-src 'self'" in headers
    html = (CLASSIC / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "<style" not in html and "style=" not in html


def test_the_web_image_builds_both_apps():
    dockerfile = (MAIN / "Dockerfile").read_text(encoding="utf-8")
    # The old screens build offline from the main build stage's package store,
    # so the production server never downloads anything for them.
    assert "FROM build AS build-classic" in dockerfile
    assert "RUN pnpm install --frozen-lockfile --offline" in dockerfile
    assert "COPY apps/add_frontend_classic/package.json apps/add_frontend_classic/pnpm-lock.yaml ./" in dockerfile
    assert "COPY --from=build /app/dist /usr/share/nginx/html\n" in dockerfile
    assert "COPY --from=build-classic /classic/dist /usr/share/nginx/html/classic" in dockerfile
    assert dockerfile.index("/app/dist /usr/share/nginx/html") < dockerfile.index("/classic/dist")


def test_the_old_screens_share_the_live_apps_lockfile():
    # The offline install above only works while the two lockfiles match.
    assert (CLASSIC / "pnpm-lock.yaml").read_bytes() == (MAIN / "pnpm-lock.yaml").read_bytes()


def test_the_old_screens_are_built_under_classic_with_their_own_title():
    vite = (CLASSIC / "vite.config.ts").read_text(encoding="utf-8")
    assert "base: '/classic/'" in vite
    assert "test:" not in vite
    html = (CLASSIC / "index.html").read_text(encoding="utf-8")
    assert "<title>ADD (old screens)</title>" in html
    assert "site.webmanifest" not in html
    classic = (CLASSIC / "src/classic.ts").read_text(encoding="utf-8")
    assert "export const CLASSIC_BASE = '/classic'" in classic
    assert re.search(r"export const CLASSIC_REMOVAL_DATE = '\d{4}-\d{2}-\d{2}'", classic)
    app = (CLASSIC / "src/App.tsx").read_text(encoding="utf-8")
    assert "{ basename: CLASSIC_BASE }" in app
    banner = (CLASSIC / "src/ClassicBanner.tsx").read_text(encoding="utf-8")
    assert "You are using the old screens. They will be removed on" in banner
    assert "Back to the new screens" in banner


def test_the_old_screens_use_the_same_api_origin_and_lockfile():
    package = json.loads((CLASSIC / "package.json").read_text(encoding="utf-8"))
    assert set(package["scripts"]) == {"dev", "build", "preview"}
    assert package["name"] == "attendance-device-dashboard-classic"
    api = (CLASSIC / "src/api.ts").read_text(encoding="utf-8")
    assert "credentials: 'include'" in api
    realtime = (CLASSIC / "src/realtime.ts").read_text(encoding="utf-8")
    assert "new EventSource('/events/v1/stream'" in realtime


def test_no_plain_link_in_the_old_screens_leaves_classic_by_accident():
    offenders = []
    for path in sorted((CLASSIC / "src").rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'href=(?:"(/[^"]*)"|\{`(/[^`]*)`\})', text):
            target = match.group(1) or match.group(2)
            if not target.startswith("/api/"):
                offenders.append(f"{path.relative_to(ROOT)}: {target}")
        if 'src="/' in text:
            offenders.append(f"{path.relative_to(ROOT)}: absolute src")
    assert offenders == []


def test_ci_builds_the_old_screens_but_does_not_test_them_as_the_live_app():
    workflow = yaml.safe_load((ROOT / ".github/workflows/add-ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["frontend"]["defaults"]["run"]["working-directory"] == "apps/add_frontend"
    classic = jobs["frontend-classic"]
    assert classic["defaults"]["run"]["working-directory"] == "apps/add_frontend_classic"
    commands = "\n".join(step.get("run", "") for step in classic["steps"])
    assert "pnpm run build" in commands
    assert "pnpm test" not in commands and "budget" not in commands and "playwright" not in commands
    assert not list((CLASSIC / "src").rglob("*.test.ts")) and not list((CLASSIC / "src").rglob("*.test.tsx"))
    assert not (CLASSIC / "tests").exists() and not (CLASSIC / "scripts").exists()


def test_removal_steps_are_written_down():
    doc = (ROOT / "docs/add-classic-screens.md").read_text(encoding="utf-8")
    for step in ("apps/add_frontend_classic", "build-classic", "location /classic/",
                 "frontend-classic", "test_classic_screens_contract.py", "add_frontend_classic\","):
        assert step in doc
