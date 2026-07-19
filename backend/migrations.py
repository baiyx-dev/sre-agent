import argparse
import json

from backend.storage.db import get_schema_status, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="SRE Agent database migration manager")
    parser.add_argument("action", choices=("upgrade", "status"))
    parser.add_argument(
        "--require-current",
        action="store_true",
        help="Exit non-zero unless all known migrations are applied and valid",
    )
    args = parser.parse_args()

    if args.action == "upgrade":
        init_db()
    status = get_schema_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if args.require_current and not status["compatible"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
