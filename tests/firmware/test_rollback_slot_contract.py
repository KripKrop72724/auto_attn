"""ZKT 2.8.1: the direct role starts storage beside an other OTA slot the
bootloader never selects (no entry, INVALID, ABORTED, PENDING_VERIFY) only
while the running image is VALID. That relies on two facts pinned here:

- a VALID image never marks itself invalid: every
  esp_ota_mark_app_invalid_rollback* call needs an unconfirmed image or
  lives in a role never built as 2.8.x (the recovery image, the factory
  trial, the 2.7.0 writer);
- the application never selects a slot itself: there is no
  esp_ota_set_boot_partition call, and only a complete, verified download
  (esp_https_ota_finish) selects the slot it wrote.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
INVALIDATE = re.compile(r"\b(esp_ota_mark_app_invalid_rollback\w*)\s*\(")

# Today's sites, exactly: file, function, call, and why a VALID 2.8.x never runs it.
SITES = (
    ("ota_manager.c", "confirm_or_report_rollback",
     "esp_ota_mark_app_invalid_rollback_and_reboot", "unconfirmed image"),
    ("ota_manager.c", "storage_recovery_boot",
     "esp_ota_mark_app_invalid_rollback_and_reboot", "recovery image"),
    ("setup_portal.c", "controller_task",
     "esp_ota_mark_app_invalid_rollback_and_reboot", "pending verify"),
    ("zkt_factory_platform.c", "zf_platform_select_factory",
     "esp_ota_mark_app_invalid_rollback", "factory trial"),
    ("zkt_reader_platform.c", "zj_reader_platform_select",
     "esp_ota_mark_app_invalid_rollback", "2.7.0 writer"),
    ("zkt_reader_platform.c", "zj_reader_platform_failed_boot",
     "esp_ota_mark_app_invalid_rollback", "2.7.0 writer"),
)


def _code(text: str) -> str:
    """Comments and literal contents blanked; offsets and newlines kept."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif text.startswith("/*", i):
            end = text.index("*/", i + 2) + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif text[i] in "\"'":
            quote, i = text[i], i + 1
            while i < n and text[i] not in (quote, "\n"):
                step = 2 if text[i] == "\\" else 1
                for j in range(i, min(i + step, n)):
                    out[j] = " "
                i += step
            i += 1
        else:
            i += 1
    return "".join(out)


def _source(name: str) -> str:
    return _code((MAIN / name).read_text(encoding="utf-8"))


def _sources():
    for path in sorted(MAIN.iterdir()):
        if path.suffix in (".c", ".h", ".in"):
            yield path.name, _code(path.read_text(encoding="utf-8"))


def _flat(text: str) -> str:
    return " ".join(text.split())


def _close(code: str, start: int, pair: str = "{}") -> int:
    """Index just past the bracket matching the one at start."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == pair[0]:
            depth += 1
        elif code[i] == pair[1]:
            depth -= 1
            if depth == 0:
                return i + 1
    raise AssertionError("unbalanced " + pair)


def _without_directives(code: str) -> str:
    """Preprocessor lines (and their continuations) blanked."""
    out, directive = [], False
    for line in code.splitlines(keepends=True):
        directive = directive or line.lstrip().startswith("#")
        out.append(re.sub(r"[^\n]", " ", line) if directive else line)
        directive = directive and line.rstrip("\n").endswith("\\")
    return "".join(out)


def _functions(code: str) -> list[tuple[str, int, int]]:
    """(name, body start, body end) of every top-level function definition."""
    plain = _without_directives(code)
    found, header, i = [], 0, 0
    while i < len(plain):
        if plain[i] == "{":
            end = _close(plain, i)
            head, depth, params = plain[header:i], 0, -1
            for j, c in enumerate(head):
                if c == "(" and depth == 0:
                    params = j
                depth += (c == "(") - (c == ")")
            name = re.search(r"(\w+)\s*$", head[:params]) if params >= 0 else None
            if name and "=" not in head:
                found.append((name.group(1), i, end))
            header = i = end
        elif plain[i] == ";":
            header = i = i + 1
        else:
            i += 1
    return found


def _definition(code: str, name: str, branch: list[str] | None = None) -> str:
    """The body of name (in that preprocessor branch) that invalidates the
    running image, or its only body."""
    bodies = [code[s:e] for n, s, e in _functions(code)
              if n == name and branch in (None, _branch(code, s))]
    calling = [b for b in bodies if INVALIDATE.search(b)] or bodies
    assert len(calling) == 1, name
    return calling[0]


def _branch(code: str, position: int) -> list[str]:
    """The #if/#elif/#else branches enclosing position, outermost first."""
    stack, offset = [], 0
    for line in code.splitlines(keepends=True):
        if offset > position:
            break
        directive = _flat(line)
        if directive.startswith("#if"):
            stack.append(directive)
        elif directive.startswith(("#elif", "#else")):
            stack[-1] = directive
        elif directive.startswith("#endif"):
            stack.pop()
        offset += len(line)
    return stack


def _guards(body: str, position: int) -> set[str]:
    """Top-level conjuncts of every if condition that governs position."""
    body = _without_directives(body)
    conjuncts = set()
    for match in re.finditer(r"\bif\s*\(", body[:position]):
        open_at = match.end() - 1
        close_at = _close(body, open_at, "()")
        rest = len(body) - len(body[close_at:].lstrip())
        end = _close(body, rest) if body[rest] == "{" else body.index(";", rest) + 1
        if not close_at <= position < end:
            continue
        condition, depth, parts, start = body[open_at + 1:close_at - 1], 0, [], 0
        for i, c in enumerate(condition):
            depth += (c == "(") - (c == ")")
            assert not (depth == 0 and condition.startswith("||", i)), condition
            if depth == 0 and condition.startswith("&&", i):
                parts.append(condition[start:i])
                start = i + 2
        conjuncts.update(_flat(part) for part in [*parts, condition[start:]])
    return conjuncts


def _runs_first(body: str, statement: str, position: int) -> bool:
    """statement sits in the function's own block before position, so every path runs it."""
    match = re.search(r"\s*".join(re.escape(token) for token in statement.split()), body)
    if not match or match.start() >= position:
        return False
    before = body[:match.start()]
    return before.count("{") - before.count("}") == 1


def _component() -> str:
    return _flat((MAIN / "CMakeLists.txt").read_text(encoding="utf-8"))


def test_every_invalidating_call_is_one_of_todays_sites() -> None:
    seen = []
    for name, code in _sources():
        functions = _functions(code)
        for call in INVALIDATE.finditer(code):
            inside = [n for n, s, e in functions if s < call.start() < e]
            assert len(inside) == 1, (name, call.group(1))
            seen.append((name, inside[0], call.group(1)))
    assert sorted(seen) == sorted(site[:3] for site in SITES)


def test_ota_manager_rolls_back_only_an_unconfirmed_image() -> None:
    code = _source("ota_manager.c")
    body = _definition(code, "confirm_or_report_rollback")
    assert "goto" not in body
    assert "!running_state_is(ESP_OTA_IMG_VALID)" in _guards(body, INVALIDATE.search(body).start())
    assert _flat(_definition(code, "running_state_is")) == (
        "{ esp_ota_img_states_t state; const esp_partition_t *running = "
        "esp_ota_get_running_partition(); return running && esp_ota_get_state_partition(running, "
        "&state) == ESP_OK && state == wanted; }")


def test_the_setup_portal_rolls_back_only_a_pending_image() -> None:
    code = _source("setup_portal.c")
    body = _definition(code, "controller_task")
    assert "goto" not in body
    assert "running_app_pending_verify()" in _guards(body, INVALIDATE.search(body).start())
    assert _flat(_definition(code, "running_app_pending_verify")) == (
        "{ const esp_partition_t *running = esp_ota_get_running_partition(); esp_ota_img_states_t "
        "state = ESP_OTA_IMG_UNDEFINED; return running && esp_ota_get_state_partition(running, "
        "&state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY; }")


def test_the_recovery_image_rollback_is_never_built_as_a_direct_image() -> None:
    code = _source("ota_manager.c")
    body = _definition(code, "storage_recovery_boot")
    call = code.index(body) + INVALIDATE.search(body).start()
    assert ("#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE"
            in _branch(code, call))
    component = _component()
    assert component.count("ZONE_LITE_STORAGE_RECOVERY_IMAGE=1") == 1
    assert ('if(ZONE_LITE_STORAGE_RECOVERY) if(NOT ZONE_LITE_FAMILY STREQUAL "zkt" OR '
            "ZONE_LITE_SEGMENTED_WRITES OR ZONE_LITE_DIRECT_LEGACY_UPGRADE OR "
            "ZONE_LITE_JOURNAL_WRITES)") in component
    assert ('if(NOT PROJECT_VER STREQUAL "2.6.27") message(FATAL_ERROR "The storage recovery '
            'image requires its exact 2.6.27 version") endif() target_compile_definitions('
            "${COMPONENT_LIB} PRIVATE ZONE_LITE_STORAGE_RECOVERY_IMAGE=1)") in component


def test_the_factory_trial_rollback_is_never_built_as_a_direct_image() -> None:
    code = _source("zkt_factory_platform.c")
    body = _definition(code, "zf_platform_select_factory")
    call = INVALIDATE.search(body).start()
    assert _branch(code, code.index(body) + call) == ["#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)"]
    # It is also pending-only: the factory fallback needs PENDING_VERIFY.
    assert "goto" not in body
    assert _runs_first(body, "if(!zf_platform_pending_fallback())return refuse(", call)
    trial = ["#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)"]
    assert _flat(_definition(code, "zf_platform_pending_fallback", trial)) == (
        "{return prepared && storage_checked && proof.state==ZF_VERIFIED && "
        "!selection_uncertain && pending() && secure();}")
    assert _flat(_definition(code, "pending", trial)) == (
        "{ const esp_partition_t *p=esp_ota_get_running_partition();esp_ota_img_states_t state; "
        "return p && p->type==ESP_PARTITION_TYPE_APP && "
        "(p->subtype==ESP_PARTITION_SUBTYPE_APP_OTA_0 || p->subtype==ESP_PARTITION_SUBTYPE_APP_OTA_1) "
        "&& esp_ota_get_state_partition(p,&state)==ESP_OK && state==ESP_OTA_IMG_PENDING_VERIFY; }")
    component = _component()
    journal = component[component.index("if(ZONE_LITE_JOURNAL_WRITES)"):]
    journal = journal[:journal.index("if(NOT CONFIG_ESP_HTTP_CLIENT_ENABLE_DIGEST_AUTH")]
    assert component.count("ZONE_LITE_FACTORY_TRIAL_IMAGE=1") == 1
    assert ('if(PROJECT_VER STREQUAL "2.6.22") target_compile_definitions(${COMPONENT_LIB} '
            "PRIVATE ZONE_LITE_FACTORY_TRIAL_IMAGE=1)") in journal
    assert ("if(ZONE_LITE_SEGMENTED_WRITES OR ZONE_LITE_DIRECT_LEGACY_UPGRADE) message(FATAL_ERROR "
            '"Journal images cannot mix legacy storage upgrade modes")') in journal


def test_the_reader_selection_rollbacks_run_only_in_the_2_7_0_writer() -> None:
    code = _source("zkt_reader_platform.c")
    version_check = "if (strcmp(env.version, ZJ_WRITER_VERSION)) return ZJ_COMPAT_VERSION;"
    for name in ("zj_reader_platform_select", "zj_reader_platform_failed_boot"):
        body = _definition(code, name)
        call = INVALIDATE.search(body).start()
        assert "goto" not in body
        steps = ("result = current_reader(", "if (result != ZJ_COMPAT_OK) return result;",
                 version_check)
        order = [_flat(body).index(_flat(step)) for step in steps]
        assert order == sorted(order), name
        assert _runs_first(body, version_check, call), name
    # env.version is the running application's own version.
    reader = _flat(_definition(code, "current_reader"))
    assert "const esp_app_desc_t *app = esp_app_get_description();" in reader
    assert ("*env = (zj_reader_environment_t){.application = app->project_name, "
            ".version = app->version,") in reader
    compat = (MAIN / "zkt_journal_compat.h").read_text(encoding="utf-8")
    assert '#define ZJ_WRITER_VERSION "2.7.0"' in compat
    # Journal roles never combine with the direct role, and the direct role is 2.8.x.
    assert ("if(ZONE_LITE_SEGMENTED_WRITES OR ZONE_LITE_DIRECT_LEGACY_UPGRADE) message(FATAL_ERROR "
            '"Journal images cannot mix legacy storage upgrade modes")') in _component()
    guard = (MAIN / "upgrade_guard.h").read_text(encoding="utf-8")
    assert re.search(r'^#define UG_DIRECT_VERSION "2\.8\.\d+"$', guard, re.M)
    assert ("if (strcmp(running->version, UG_DIRECT_VERSION)) return failed("
            in _flat(_source("storage_upgrade.c")))


def test_the_application_never_selects_a_boot_slot_itself() -> None:
    finishes = []
    for name, code in _sources():
        assert re.search(r"\besp_ota_set_boot_partition\s*\(", code) is None, name
        finishes += [name] * len(re.findall(r"\besp_https_ota_finish\s*\(", code))
    assert finishes == ["ota_manager.c"]
