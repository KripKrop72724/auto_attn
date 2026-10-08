"""Generate the fixed writer matrix header or print its signable contract.

No input policy path or environment override is accepted. The reviewed package
policy is the only source; tests invoke pure functions with synthetic fixtures.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/add_backend"))
from zk_add.zkt_reader_matrix import (  # noqa: E402
    canonical, load_matrix, matrix_hash, matrix_marker, minimum_reader_version, validate_matrix, writer_matrix_contract,
)


def render_header(matrix):
    matrix = validate_matrix(matrix)
    rows = matrix["readers"]
    lines = ["/* Generated from the reviewed reader matrix; do not edit. */", "#pragma once",
             f'#define ZJ_READER_MATRIX_SHA256 "{matrix_hash(matrix)}"',
             f'#define ZJ_READER_MATRIX_MARKER "{matrix_marker(matrix)}"',
             f"#define ZJ_READER_MATRIX_COUNT {len(rows)}U",
             "typedef struct { const char *version; unsigned char digest[32]; } zj_matrix_entry_t;",
             "static const zj_matrix_entry_t zj_matrix_entries[] = {"]
    for row in rows:
        digest = ",".join("0x" + row["application_sha256"][i:i + 2] for i in range(0, 64, 2))
        lines.append('    {"' + row["version"] + '", {' + digest + "}},")
    if not rows:
        lines.append("    {0, {0}}, /* Not an admitted entry; count is zero. */")
    lines.extend(["};", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--header", type=Path)
    group.add_argument("--signing-contract", action="store_true")
    group.add_argument("--validate-manifest", type=Path)
    args = parser.parse_args()
    try:
        matrix = load_matrix(require_pinned=args.signing_contract or bool(args.validate_manifest))
        if args.header:
            args.header.write_text(render_header(matrix), encoding="ascii")
        elif args.validate_manifest:
            if args.validate_manifest.stat().st_size > 65536:
                raise ValueError()
            manifest = json.loads(args.validate_manifest.read_text())
            expected = writer_matrix_contract(matrix)
            if (canonical(manifest.get("queue_storage")) != canonical(expected)
                    or manifest.get("minimum_bootstrap_version") != minimum_reader_version(matrix)
                    or manifest.get("version") != "2.7.0"
                    or manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"):
                raise ValueError()
        else:
            print(canonical(writer_matrix_contract(matrix)).decode())
        return 0
    except (OSError, ValueError):
        print("Reader matrix is unavailable, unpopulated or invalid.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
