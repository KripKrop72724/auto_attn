"""Synthetic CI boundary diagnostic; emit only bounded shape/encoding metadata."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps/add_backend"))
from zk_add.zkt_factory_contract import factory_trial_targets  # noqa: E402


def diagnose(raw):
    result = {"bytes": len(raw), "over_limit": len(raw) > 4096,
              "bom": "NONE", "trailing_crlf": raw.endswith(b"\r\n"), "utf8": False}
    for name, prefix in (("UTF8", b"\xef\xbb\xbf"), ("UTF16LE", b"\xff\xfe"),
                         ("UTF16BE", b"\xfe\xff")):
        if raw.startswith(prefix):
            result["bom"] = name
    try:
        text = raw.decode("utf-8")
        result["utf8"] = True
        value = json.loads(text)
    except (ValueError, UnicodeError):
        result["json"] = False
        return result
    result.update(json=True, root_type=type(value).__name__)
    if isinstance(value, list):
        result["rows"] = len(value)
        result["row_types"] = [type(row).__name__ for row in value[:4]]
        keys = {"connector_id", "mac", "terminal_serial"}
        expected = factory_trial_targets()
        result["keys_exact"] = [isinstance(row, dict) and set(row) == keys for row in value[:4]]
        result["matches_position"] = [row == expected[i] if i < len(expected) else False
                                       for i, row in enumerate(value[:4])]
        result["exact_prefix"] = 1 <= len(value) <= 3 and value == expected[:len(value)]
    return result


if __name__ == "__main__":
    print("FACTORY_SCOPE_INPUT " + json.dumps(diagnose(sys.stdin.buffer.read(4097)), sort_keys=True))
