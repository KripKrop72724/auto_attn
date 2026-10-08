"""Emit the reviewed factory policy or verify its exact publication scope."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/add_backend"))
from zk_add.zkt_factory_contract import (  # noqa: E402
    factory_trial_exposure, factory_trial_signing_contract, validate_factory_trial_contract,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    exposure = parser.add_mutually_exclusive_group()
    exposure.add_argument("--exposure")
    exposure.add_argument("--exposure-stdin", action="store_true")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    try:
        raw_exposure = args.exposure
        if args.exposure_stdin:
            # Windows PowerShell 5.1 removes native argument quotes. Keep the
            # JSON document off that boundary, with a bounded UTF-8 stdin read.
            raw_exposure = sys.stdin.buffer.read(4097)
        if raw_exposure is not None:
            if len(raw_exposure) > 4096:
                raise ValueError()
            if isinstance(raw_exposure, bytes):
                raw_exposure = raw_exposure.decode("utf-8")
            factory_trial_exposure(json.loads(raw_exposure))
        if args.manifest:
            if args.manifest.stat().st_size > 65536:
                raise ValueError()
            value = json.loads(args.manifest.read_text())
            validate_factory_trial_contract(value.get("factory_trial"))
            if (value.get("version") != "2.6.22" or value.get("minimum_bootstrap_version") != "2.5.2"
                    or value.get("queue_storage", {}).get("allowed_bootstrap_versions") != []
                    or value.get("queue_storage", {}).get("allowed_bootstrap_images") != {}):
                raise ValueError()
        if args.manifest is None:
            print(json.dumps(factory_trial_signing_contract(), sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, AttributeError):
        print("Factory trial policy or exact exposure is invalid.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
