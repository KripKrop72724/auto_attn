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
    parser.add_argument("--exposure")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.exposure is not None:
            if len(args.exposure) > 4096:
                raise ValueError()
            factory_trial_exposure(json.loads(args.exposure))
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
